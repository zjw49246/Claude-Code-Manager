# 一键更新重启 — 详细设计文档

## 1. 概述

### 1.1 目标

提供一个前端按钮，点击后自动完成「拉取最新代码 → 同步依赖 → 数据库迁移 → 构建前端 → 重启服务」的完整部署流程，替代手动 SSH 执行多条命令。

### 1.2 当前手动部署流程

```bash
cd /home/ubuntu/Claude-Code-Manager
git pull origin main
./scripts/refresh_pty.sh          # 更新 claude-pty 依赖
uv sync                           # 同步 Python 依赖
alembic upgrade head              # 数据库迁移
cd frontend && npm run build      # 构建前端
systemctl --user restart ccm.service  # 重启服务
```

### 1.3 核心风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 新代码有 bug，服务起不来 | 服务不可用 | 记录旧 commit，提供回滚入口 |
| 数据库迁移失败 | DB 处于不一致状态 | 迁移前自动备份数据库 |
| 破坏性迁移（删列/改类型） | 回滚代码也无法恢复数据 | 备份 + 迁移前检测 |
| 迁移期间旧代码并发读写 | 请求报错 | 有迁移时先停服务再迁移 |
| 构建/依赖安装耗时长 | 用户等待焦虑 | WebSocket 实时推送日志 |

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────┐
│  前端 (React)                                            │
│                                                          │
│  [更新并重启] 按钮 → 确认对话框 → POST /api/system/update │
│       ↑                                                  │
│       │ WebSocket (channel: "system_update")              │
│       │ 实时接收每一步的日志和状态                          │
│       │                                                  │
│  重启后 → 轮询 /api/system/health 直到恢复                │
└─────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│  后端 (FastAPI)                                          │
│                                                          │
│  POST /api/system/update                                 │
│       │                                                  │
│       ▼                                                  │
│  UpdateService (异步执行更新流水线)                        │
│       │                                                  │
│       ├─ ① git pull                                      │
│       ├─ ② 检测新 migration                              │
│       ├─ ③ 备份数据库（每次都备份，SQLite 成本极低）       │
│       ├─ ④ uv sync（先同步基础依赖）                      │
│       ├─ ⑤ refresh_pty.sh（后装 PTY，防止被 sync 覆盖）   │
│       ├─ ⑥ npm install（如 package.json 有变更）          │
│       ├─ ⑦ npm run build                                 │
│       ├─ ⑧ 停止服务（如有新 migration，防止并发读写）      │
│       ├─ ⑨ alembic upgrade head（如有新 migration）       │
│       └─ ⑩ 启动/重启服务（脱离当前进程）                   │
│                                                          │
│  WebSocketBroadcaster ← 每步结果实时广播                  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 后端设计

### 3.1 API 端点

#### `POST /api/system/update`

触发更新流水线。

**请求：**
```json
{
  "skip_frontend_build": false,   // 可选，跳过前端构建（仅后端改动时加速）
  "dry_run": false,               // 可选，仅检测是否有更新，不执行
  "force": false                  // 可选，即使 "Already up to date" 也强制执行后续步骤
}
```

**dry_run 模式**：仅执行 `git fetch origin main`（不 pull），然后对比本地 HEAD 与 origin/main 的差异，返回：
```json
{
  "has_updates": true,
  "commits_behind": 3,
  "has_new_migrations": true,
  "migration_count": 1,
  "has_frontend_changes": true,
  "has_package_changes": false,
  "current_commit": "abc1234",
  "latest_commit": "def5678",
  "commit_messages": ["fix: ...", "feat: ...", "chore: ..."]
}
```
可用于前端在确认对话框中展示「待更新内容预览」。

**响应（立即返回）：**
```json
{
  "update_id": "upd_20260622_143052",
  "status": "started",
  "message": "更新流水线已启动，请通过 WebSocket 订阅 system_update 频道查看进度"
}
```

**错误响应：**
```json
// 409 Conflict — 已有更新在执行
{
  "detail": "更新正在进行中",
  "update_id": "upd_20260622_143052"
}
```

#### `GET /api/system/update/status`

查询当前/最近一次更新的状态（供前端重连后恢复）。

**响应：**
```json
{
  "update_id": "upd_20260622_143052",
  "status": "running",           // idle | running | completed | failed | rolled_back
  "current_step": 5,
  "total_steps": 10,
  "steps": [
    {"name": "git_pull", "status": "completed", "duration_ms": 1200},
    {"name": "detect_changes", "status": "completed", "duration_ms": 50, "result": {"has_new_migrations": true, "migration_count": 2, "has_frontend_changes": true, "has_package_changes": false}},
    {"name": "backup_database", "status": "completed", "duration_ms": 300},
    {"name": "uv_sync", "status": "completed", "duration_ms": 8000},
    {"name": "refresh_pty", "status": "completed", "duration_ms": 5000},
    {"name": "npm_install", "status": "skipped"},
    {"name": "frontend_build", "status": "running", "started_at": "2026-06-22T14:31:15Z"},
    {"name": "stop_service", "status": "pending"},
    {"name": "alembic_upgrade", "status": "pending"},
    {"name": "start_service", "status": "pending"}
  ],
  "old_commit": "abc1234",
  "new_commit": "def5678",
  "started_at": "2026-06-22T14:30:52Z"
}
```

#### `POST /api/system/update/rollback`

手动触发回滚到上次更新前的状态。

**行为：**
1. 恢复数据库备份（如有）
2. `git reset --hard {old_commit}`（保持在 main 分支）
3. `uv sync`（恢复旧版本依赖）
4. 重启服务

### 3.2 WebSocket 事件

通过现有 `WebSocketBroadcaster` 广播到 `system_update` 频道。

**事件格式：**
```json
{
  "channel": "system_update",
  "data": {
    "event": "step_update",        // step_update | log_line | update_complete | update_failed | restarting
    "update_id": "upd_20260622_143052",
    "step": "uv_sync",
    "status": "running",           // running | completed | failed | skipped
    "message": "正在同步 Python 依赖...",
    "log": "Resolved 142 packages in 3.2s",  // 命令的 stdout/stderr 输出
    "progress": 62                 // 百分比（可选）
  }
}
```

**特殊事件 — 即将重启：**
```json
{
  "channel": "system_update",
  "data": {
    "event": "restarting",
    "message": "服务即将重启，请等待自动重连..."
  }
}
```

### 3.3 更新服务 (`UpdateService`)

新建 `backend/services/update_service.py`：

```python
class UpdateService:
    """管理一键更新重启的完整流水线。"""

    def __init__(self, broadcaster: WebSocketBroadcaster):
        self.broadcaster = broadcaster
        self._lock = asyncio.Lock()        # 防止并发更新
        self._current_update: UpdateState | None = None

    async def start_update(self, skip_frontend_build=False, dry_run=False, force=False) -> UpdateState:
        """启动更新流水线（异步，立即返回）。"""

    async def get_status(self) -> UpdateState | None:
        """获取当前/最近一次更新状态。"""

    async def rollback(self) -> None:
        """回滚到更新前的状态。"""
```

### 3.4 更新流水线详细步骤

```
开始
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 1: git pull                                         │
│                                                          │
│   old_commit = git rev-parse HEAD                        │
│   git fetch origin main                                  │
│   git_output = git pull origin main                      │
│                                                          │
│   如果 "Already up to date" 且非 force:                    │
│     → 返回 "无需更新"                                     │
│                                                          │
│   如果 pull 失败（本地有未提交改动等）：                     │
│     → 报错终止，不做任何变更                                │
│                                                          │
│   new_commit = git rev-parse HEAD                        │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 2: 检测新 migration                                  │
│                                                          │
│   git diff --name-only {old_commit}..{new_commit}        │
│       -- alembic/versions/                               │
│                                                          │
│   has_new_migrations = len(changed_files) > 0            │
│   如果有新 migration，记录文件列表                          │
│                                                          │
│   同时检测 frontend/ 是否有变更：                           │
│   has_frontend_changes = git diff 中包含 frontend/ 文件   │
│   同时检测 package.json 是否有变更：                       │
│   has_package_changes = "frontend/package.json" in diff   │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 3: 备份数据库（每次都备份）                            │
│                                                          │
│   SQLite 文件拷贝成本极低（通常 < 500ms），每次都备份       │
│   不仅防迁移失败，也防新代码运行时逻辑导致数据问题           │
│                                                          │
│   数据库类型判断：                                         │
│   - SQLite: 必须用 SQLite 备份 API，不能简单 cp！          │
│     sqlite3 claude_manager.db                            │
│       ".backup backups/claude_manager.db.bak.{timestamp}"│
│     原因：SQLite WAL 模式下，未 checkpoint 的数据在        │
│     .db-wal 文件中，cp 只拷贝主文件会丢数据               │
│   - PostgreSQL: pg_dump > backups/pg_backup_{ts}.sql     │
│                                                          │
│   备份文件保留最近 5 个，自动清理旧备份                      │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 4: uv sync（先同步基础依赖）                          │
│                                                          │
│   执行 uv sync                                            │
│   实时推送 stdout 到 WebSocket                             │
│                                                          │
│   注意：必须在 refresh_pty.sh 之前执行！                    │
│   因为 uv sync 按 uv.lock 同步，会把 claude-pty 设为       │
│   lock 中的版本。之后 refresh_pty.sh 再覆盖为最新版本。     │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 5: refresh_pty.sh（后执行，防止被 uv sync 覆盖）      │
│                                                          │
│   执行 ./scripts/refresh_pty.sh                           │
│   实时推送 stdout 到 WebSocket                             │
│                                                          │
│   该脚本用 uv pip install --force-reinstall 单独安装       │
│   claude-pty 到 main 分支最新 commit，必须在 uv sync 之后  │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 6: npm install（仅当 package.json 有变更）            │
│                                                          │
│   如果 has_package_changes:                               │
│     cd frontend && npm install                            │
│   否则跳过（status: skipped）                              │
│                                                          │
│   新增/更新前端依赖时必须先 install 再 build               │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 7: 构建前端                                          │
│                                                          │
│   cd frontend && npm run build                            │
│                                                          │
│   如果 skip_frontend_build=true → 跳过                    │
│   如果 has_frontend_changes=false → 跳过                  │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 8: 停止服务（仅当 has_new_migrations = true）         │
│                                                          │
│   systemctl --user stop ccm.service                      │
│                                                          │
│   必须在迁移前停止服务，原因：                               │
│   - SQLite 使用文件锁，运行中的服务占用 DB 会导致            │
│     alembic 报 "database is locked"                       │
│   - 即使迁移成功，旧代码读到新表结构也会出错                 │
│                                                          │
│   无新 migration → 跳过（服务继续运行，最后一步 restart）    │
│                                                          │
│   注意：停止服务后当前 API 请求会断开！                      │
│   所以这一步之前必须确保 WebSocket 已通知前端 "即将停服"，   │
│   前端进入轮询模式。                                       │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 9: alembic upgrade head（仅当 has_new_migrations）    │
│                                                          │
│   执行 uv run alembic upgrade head                        │
│                                                          │
│   如果失败：                                               │
│     → 恢复数据库备份                                       │
│     → git reset --hard {old_commit}（保持在 main 分支）    │
│     → uv sync（恢复旧依赖）                                │
│     → 启动服务（用旧代码）                                  │
│     → 写入回滚状态文件（供前端重连后读取）                    │
│     → 终止流水线                                           │
│                                                          │
│   无新 migration → 跳过此步（status: skipped）             │
└──────────────────────────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│ Step 10: 启动/重启服务                                    │
│                                                          │
│   ■ 有迁移路径（Step 8-10 由外部脚本执行）：                │
│     update_migrate.sh 中的 systemctl start               │
│     因为 Step 8 的 stop 会杀掉当前 Python 进程，           │
│     所以 Step 8/9/10 必须封装在外部脚本中，                │
│     通过 nohup 脱离当前进程执行。                          │
│     见 3.5 节「停服-迁移-启动的执行策略」。                 │
│                                                          │
│   ■ 无迁移路径（仅 Step 10 需要脱离进程）：                 │
│     1. 写状态文件：status="restarting"，包含                │
│        old_commit、new_commit（供重启后恢复）               │
│     2. 广播 "restarting" 事件                              │
│     3. 等待 1 秒（让 WebSocket 消息发出去）                 │
│     4. 通过 nohup 脱离当前进程执行 restart：                │
│        nohup bash -c "sleep 2 && systemctl --user         │
│          restart ccm.service && echo completed > \        │
│          /tmp/ccm-restart-ok-{port}" \                    │
│          > /tmp/ccm-restart-{port}.log 2>&1 &             │
│     重启命令必须脱离当前进程，否则进程被杀后命令也终止。     │
│     5. UpdateService 启动时检查状态文件，如果 status 为     │
│        "restarting" 则标记为 "completed"（说明重启成功）    │
└──────────────────────────────────────────────────────────┘
```

### 3.5 停服-迁移-启动的执行策略（关键设计）

**核心矛盾：** 当有新 migration 时，需要先停服务再迁移。但 `systemctl --user stop ccm.service` 会杀掉当前进程（包括正在执行更新流水线的 asyncio task），导致后续的迁移和启动步骤无法执行。

**解决方案：** 将「停服 → 迁移 → 启动」封装为一个**外部脚本**，通过 `nohup` 脱离当前进程执行。

```bash
#!/bin/bash
# scripts/update_migrate.sh — 停服-迁移-启动外部脚本
set -uo pipefail  # 不用 -e，需要手动处理迁移失败

PROJECT_DIR="$1"
OLD_COMMIT="$2"
BACKUP_FILE="$3"
PORT="$4"
SERVICE_NAME="ccm.service"
STATUS_FILE="/tmp/ccm-update-status-${PORT}.json"
LOG_FILE="/tmp/ccm-update-migrate-${PORT}.log"

cd "$PROJECT_DIR"

write_status() {
    local status="$1" message="$2" step="${3:-}"
    cat > "$STATUS_FILE" <<EOJSON
{
  "status": "$status",
  "message": "$message",
  "step": "$step",
  "old_commit": "$OLD_COMMIT",
  "backup_file": "$BACKUP_FILE",
  "port": $PORT,
  "timestamp": "$(date -Iseconds)"
}
EOJSON
}

# 1. 停止服务
write_status "stopping" "正在停止服务..." "stop_service"
if ! systemctl --user stop "$SERVICE_NAME"; then
    write_status "failed" "停止服务失败，中止迁移" "stop_service"
    # 服务可能仍在运行，不能继续迁移
    exit 1
fi
sleep 1

# 2. 执行迁移（带超时，输出记录到日志文件）
write_status "migrating" "正在执行数据库迁移..." "alembic_upgrade"
if timeout 120 uv run alembic upgrade head >> "$LOG_FILE" 2>&1; then
    write_status "starting" "迁移成功，正在启动服务..." "start_service"
    systemctl --user start "$SERVICE_NAME"
    write_status "completed" "更新完成" "start_service"
else
    EXIT_CODE=$?
    # 迁移失败，回滚
    write_status "rolling_back" "迁移失败(exit=$EXIT_CODE)，正在回滚..." "alembic_upgrade"
    # 恢复备份时必须删除 WAL/SHM 残留文件，否则 SQLite 会尝试 replay 旧 WAL
    rm -f claude_manager.db-wal claude_manager.db-shm
    cp "$BACKUP_FILE" claude_manager.db
    git reset --hard "$OLD_COMMIT"
    uv sync >> "$LOG_FILE" 2>&1 || true  # 依赖恢复失败不阻塞启动
    systemctl --user start "$SERVICE_NAME"
    write_status "rolled_back" "迁移失败，已回滚到 $OLD_COMMIT，详见 $LOG_FILE" "alembic_upgrade"
fi
```

**前端恢复机制：**
- 服务停止后，前端 WebSocket 断开，进入轮询 `/api/system/health`
- 服务重启后，前端读取 `GET /api/system/update/status`（从状态文件恢复）获取最终结果
- `UpdateService` 启动时检查 `/tmp/ccm-update-status-{port}.json`，将结果加载到内存
- 状态文件包含 `step` 字段，`UpdateService` 据此回填 steps 数组中 Step 8-10 的最终状态
- 状态文件包含 `old_commit` 和 `backup_file`，供手动回滚 API 使用

**两条执行路径总结：**

```
无新 migration（快速路径）：
  Step 1-7 在当前进程内执行
  → Step 10: nohup restart（当前进程被杀，前端轮询等待恢复）

有新 migration（安全路径）：
  Step 1-7 在当前进程内执行
  → 广播 "即将停服迁移"
  → nohup scripts/update_migrate.sh {project_dir} {old_commit} {backup_file} {port}
  → 当前进程被 stop 杀掉
  → 脚本继续：迁移 → 启动（或回滚 → 启动）
  → 前端轮询恢复后读取状态文件
```

### 3.6 迁移失败回滚流程

回滚由 `scripts/update_migrate.sh` 自动执行（见 3.5），流程：

```
alembic upgrade head 失败
  │
  ▼
┌────────────────────────────────────────────┐
│ 1. 恢复数据库备份                           │
│    cp backups/xxx.bak → claude_manager.db   │
│                                            │
│ 2. 回退代码（保持在 main 分支）              │
│    git reset --hard {old_commit}            │
│    注意：不用 git checkout，避免 detached    │
│    HEAD 导致后续 git pull 失败               │
│                                            │
│ 3. 恢复依赖（旧代码可能需要旧依赖）          │
│    uv sync                                 │
│                                            │
│ 4. 启动服务（用旧版本代码）                  │
│    systemctl --user start ccm.service      │
│                                            │
│ 5. 写入状态文件                              │
│    /tmp/ccm-update-status-{port}.json             │
│    status: "rolled_back"                   │
│    前端重连后通过 API 读取此状态              │
└────────────────────────────────────────────┘
```

### 3.7 手动回滚 API

#### `POST /api/system/update/rollback`

手动触发回滚到上次更新前的状态（用于更新成功但新版本有运行时 bug 的场景）。

**数据来源：** `old_commit` 和 `backup_file` 从 `/tmp/ccm-update-status-{port}.json` 状态文件读取（`UpdateService` 启动时加载到内存）。

**行为：**
1. 恢复数据库备份（如有）：删除 `-wal`/`-shm` 残留 → `cp backup → claude_manager.db`
2. `git reset --hard {old_commit}`（保持在 main 分支，不用 checkout 避免 detached HEAD）
3. `uv sync`（恢复旧版本依赖）
4. 写状态文件：`status: "rolling_back"`
5. 通过 nohup 脱离当前进程执行 `systemctl --user restart ccm.service`（与更新的快速路径相同，需要脱离进程避免自杀问题）
6. `UpdateService` 重启后从状态文件读取，标记为 `rolled_back`

### 3.8 并发控制

- 使用 `asyncio.Lock` 确保同一时间只有一个更新流水线在运行
- 如果已有更新在执行，返回 `409 Conflict`
- 更新状态通过文件持久化（`/tmp/ccm-update-status-{port}.json`），服务重启后可恢复最终状态

### 3.9 超时控制

| 步骤 | 超时时间 | 超时行为 |
|------|----------|----------|
| git pull | 60s | 终止，报错 |
| uv sync | 300s | 终止，报错 |
| refresh_pty | 120s | 终止，报错 |
| npm install | 120s | 终止，报错 |
| npm run build | 300s | 终止，报错（不回滚，代码已是新的） |
| alembic upgrade | 120s | 终止，回滚（由外部脚本处理） |

### 3.10 多实例场景

当前设计**仅更新和重启当前实例**。如果使用 `start_all.sh` 多实例部署（共享同一 git 仓库），需注意：

- `git pull` 会影响共享仓库，其他实例的磁盘代码也变了
- 但其他实例的进程仍在运行旧代码（Python 模块已加载到内存）
- 其他实例需要手动重启才能加载新代码
- **V1 不支持批量更新**：仅更新触发按钮所在的实例
- **未来可扩展**：通过广播通知其他实例自行重启

---

## 4. 前端设计

### 4.1 按钮位置

在 `Header.tsx` 的设置/偏好区域添加「更新并重启」按钮，使用 `RefreshCw` 图标（来自 lucide-react）。

### 4.2 交互流程

```
用户点击 [🔄 更新并重启]
        │
        ▼
┌─────────────────────────────────┐
│  确认对话框                       │
│                                  │
│  "确定要拉取最新代码并重启服务吗？ │
│   更新期间服务可能短暂不可用。"    │
│                                  │
│  [ ] 跳过前端构建（仅后端更新）    │
│                                  │
│  [取消]  [确认更新]               │
└─────────────────────────────────┘
        │ 确认
        ▼
┌─────────────────────────────────┐
│  更新进度面板（Modal）             │
│                                  │
│  ● git pull           ✅ 1.2s    │
│  ● 检测变更            ✅ 2个迁移 │
│  ● 备份数据库          ✅ 0.3s    │
│  ● 同步 Python 依赖    ✅ 8.0s   │
│  ● 更新 PTY 依赖       ✅ 5.0s   │
│  ● npm install         ⏭ 跳过    │
│  ● 构建前端            ⏳ 运行中  │
│  ○ 停止服务            ⏸ 等待中   │
│  ○ 数据库迁移          ⏸ 等待中   │
│  ○ 启动服务            ⏸ 等待中   │
│                                  │
│  ┌─ 实时日志 ────────────────┐   │
│  │ Resolved 142 packages...  │   │
│  │ Installing dependencies.. │   │
│  │ ✓ Installed 3 packages    │   │
│  └───────────────────────────┘   │
└─────────────────────────────────┘
        │ 收到 "restarting" 事件
        ▼
┌─────────────────────────────────┐
│  重连等待界面                     │
│                                  │
│  🔄 服务正在重启...               │
│                                  │
│  每 2 秒轮询 /api/system/health   │
│  最多等待 60 秒                   │
└─────────────────────────────────┘
        │ health 返回 200
        ▼
┌─────────────────────────────────┐
│  更新成功提示                     │
│                                  │
│  ✅ 更新完成！                    │
│  abc1234 → def5678               │
│  耗时: 45s                       │
│                                  │
│  [关闭]  [查看变更日志]            │
└─────────────────────────────────┘
```

### 4.3 异常情况处理

**迁移失败（已自动回滚）：**
```
┌─────────────────────────────────┐
│  ❌ 更新失败                      │
│                                  │
│  数据库迁移失败，已自动回滚到       │
│  旧版本 (abc1234)                │
│                                  │
│  错误信息：                       │
│  alembic.util.exc.CommandError:  │
│  Can't locate revision...        │
│                                  │
│  [关闭]  [查看完整日志]            │
└─────────────────────────────────┘
```

**重启后无法恢复：**
```
┌─────────────────────────────────┐
│  ⚠️ 服务重启超时                  │
│                                  │
│  已等待 60 秒，服务仍未响应。      │
│  可能需要手动检查：                │
│                                  │
│  ssh → journalctl --user         │
│       -u ccm.service -n 50       │
│                                  │
│  旧版本 commit: abc1234          │
│  手动回滚：                       │
│  git reset --hard abc1234 &&     │
│  uv sync &&                      │
│  systemctl --user restart        │
│  ccm.service                     │
│                                  │
│  [关闭]  [复制回滚命令]            │
└─────────────────────────────────┘
```

### 4.4 新增组件

```
frontend/src/components/System/
├── UpdateButton.tsx         # Header 中的触发按钮
├── UpdateConfirmDialog.tsx  # 确认对话框
├── UpdateProgressModal.tsx  # 进度面板 + 实时日志
└── UpdateReconnect.tsx      # 重连等待 + 结果展示
```

### 4.5 前端状态管理

```typescript
interface UpdateState {
  status: 'idle' | 'confirming' | 'running' | 'restarting' | 'completed' | 'failed';
  updateId: string | null;
  steps: StepInfo[];
  logs: string[];
  oldCommit: string | null;
  newCommit: string | null;
  error: string | null;
}

// 使用 useState + useEffect(WebSocket) 管理
// 不需要全局状态管理，因为更新是低频操作且只在一个地方触发
```

---

## 5. 文件变更清单

### 后端新增

| 文件 | 说明 |
|------|------|
| `backend/services/update_service.py` | 更新流水线核心逻辑 |
| `scripts/update_migrate.sh` | 停服-迁移-启动外部脚本（由 UpdateService 动态生成或静态模板） |

### 后端修改

| 文件 | 说明 |
|------|------|
| `backend/api/system.py` | 添加 3 个端点：`update`、`update/status`、`update/rollback` |
| `backend/main.py` | 初始化 `UpdateService` 单例；启动时检查 `/tmp/ccm-update-status-{port}.json` 恢复状态 |

### 前端新增

| 文件 | 说明 |
|------|------|
| `frontend/src/components/System/UpdateButton.tsx` | Header 中的触发按钮 |
| `frontend/src/components/System/UpdateConfirmDialog.tsx` | 确认对话框（含 dry_run 预览、选项勾选） |
| `frontend/src/components/System/UpdateProgressModal.tsx` | 进度面板 + 实时日志 |
| `frontend/src/components/System/UpdateReconnect.tsx` | 重连等待 + 最终结果展示 |

### 前端修改

| 文件 | 说明 |
|------|------|
| `frontend/src/components/Layout/Header.tsx` | 嵌入 `UpdateButton` |
| `frontend/src/api/client.ts` | 添加 `startUpdate()`、`getUpdateStatus()` API 方法 |

---

## 6. 安全考虑

1. **Auth 校验**：所有更新相关端点必须通过 Bearer Token 认证（复用现有 auth 中间件）
2. **防误触**：前端二次确认对话框
3. **并发锁**：同一时间只允许一个更新流水线
4. **命令注入**：所有 subprocess 调用使用参数列表形式（非 shell=True），不拼接用户输入
5. **备份保留**：只保留最近 5 个备份，防止磁盘空间耗尽

---

## 7. 测试方案

### 7.1 单元测试

- `UpdateService` 的每个步骤独立可测（mock subprocess）
- 迁移检测逻辑（有/无新 migration 文件）
- 回滚逻辑（模拟迁移失败后的恢复流程）

### 7.2 集成测试

- 在测试环境执行完整更新流程
- 模拟 alembic 失败，验证数据库恢复
- 验证 WebSocket 事件格式和顺序

### 7.3 手动测试

- 正常更新（无 migration）→ 快速路径
- 正常更新（有 migration）→ 完整路径
- 模拟 git pull 冲突
- 模拟迁移失败 → 自动回滚
- 重启后前端自动重连
- 并发请求（第二个应返回 409）

---

## 8. 未来扩展（暂不实现）

- **定时自动更新**：cron 定期检查是否有新 commit，自动更新
- **灰度更新**：多实例场景下逐个更新
- **更新历史**：记录每次更新的 commit 范围、耗时、是否成功
- **Changelog 展示**：解析 git log 显示更新了哪些功能
