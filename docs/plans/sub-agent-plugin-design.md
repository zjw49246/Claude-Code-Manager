# Sub-Agent 插件设计（替代 Native Agent）

## 背景

### Native Agent 的限制

Claude Code 内置的 Agent/Task 工具（Native Agent）存在以下问题：

1. **不可控**：CCM 无法干预 Native Agent 的执行过程、资源使用和超时行为
2. **中间信息不实时**：Native Agent 执行期间，主 session 看不到中间进度，只能等最终结果
3. **完成后不通知主 session**：结果以 tool_result 静默返回，不触发任何 WebSocket 事件，前端无感知
4. **无法传递上下文**：Native Agent 运行在隔离环境，拿不到 CCM 的项目信息、task 配置、历史对话等上下文
5. **不可观测**：PTY 模式下虽然能通过 `subagents/` 目录镜像为 `native-agent` 记录，但只是事后追溯，无法实时介入

### 目标

设计一个类似 Monitor 的 Sub-Agent 插件系统，面向**一次性任务执行**（调研、代码审查、依赖分析等）。主 Agent 通过 MCP 工具创建 Sub-Agent，Sub-Agent 作为持久 Claude 子进程自主运行，实时上报进度，完成后将结果注入主 session 唤醒主 Agent。

---

## 架构设计

```
Task（主 Agent Session）
  │
  ├─ Skills MCP Server (ccm_skills_server.py)
  │   ├─ create_sub_agent(name, prompt, context)
  │   │       │
  │   │       ▼
  │   │   HTTP POST /api/tasks/{id}/sub-agent-sessions
  │   │       │
  │   │       ▼
  │   │   Dispatcher 启动 Sub-Agent 子进程
  │   │       │
  │   │       ▼
  │   │   ┌──────────────────────────────────────────┐
  │   │   │  Sub-Agent 子进程 (claude -p ...)         │
  │   │   │                                          │
  │   │   │  MCP Server: ccm_sub_agent_server.py     │
  │   │   │   ├─ report_progress(summary)            │
  │   │   │   │    → HTTP POST /checks               │
  │   │   │   │    → WS 广播 sub_agent_progress      │
  │   │   │   ├─ submit_result(result, status)       │
  │   │   │   │    → HTTP POST /complete             │
  │   │   │   │    → enqueue_message → 主 session     │
  │   │   │   │    → WS 广播 sub_agent_completed     │
  │   │   │   └─ get_context()                       │
  │   │   │        → HTTP GET /context               │
  │   │   │        → 返回 task 信息 + 项目上下文      │
  │   │   └──────────────────────────────────────────┘
  │   │
  │   ├─ check_sub_agents()
  │   │       → HTTP GET /api/tasks/{id}/sub-agent-sessions?type=sub_agent
  │   │       → 返回所有 Sub-Agent 状态 + 最新进度/结果
  │   │
  │   └─ stop_sub_agent(session_id)
  │           → HTTP DELETE /api/tasks/{id}/sub-agent-sessions/{sid}
  │           → SIGTERM 子进程 → 清理
  │
  └─ --disallowedTools Agent,Task
       禁用 Native Agent，强制走 Sub-Agent 插件
```

---

## 与 Monitor 的区别

| 维度 | Monitor | Sub-Agent |
|------|---------|-----------|
| **用途** | 持续后台监控（编译、测试、日志） | 一次性任务执行（调研、审查、分析） |
| **生命周期** | 长期运行，主动 `mark_complete` 或手动停止 | 有明确结束点，`submit_result` 后自动结束 |
| **agent_type** | `monitor` | `sub_agent` |
| **source** | `ccm` | `ccm` |
| **完成行为** | 通知主 Agent（可选） | **必须**将结果注入主 session 唤醒主 Agent |
| **中间上报** | `report_status(summary, is_important)` | `report_progress(summary)` — 始终以 system_event 展示 |
| **结果注入** | `is_important=True` 时注入 | `submit_result` 始终注入 |
| **并发上限** | 每 task 5 个 | 每 task 3 个 |
| **超时** | 4 小时 | 2 小时 |
| **典型 prompt** | "每 30 秒检查编译状态" | "审查 PR #42 的安全风险并给出报告" |
| **MCP Server** | `ccm_monitor_agent_server.py` | `ccm_sub_agent_server.py`（新建） |

---

## 数据模型

复用 `sub_agent_sessions` 表（`agent_type="sub_agent"`），**无需新建表**。

### 现有表结构（`sub_agent_sessions`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增 ID |
| `task_id` | Integer FK | 关联 Task |
| `agent_type` | String | `"monitor"` / `"sub_agent"` / `"native-agent"` |
| `source` | String | `"ccm"` / `"native"` |
| `name` | String | 用户可读名称 |
| `prompt` | Text | 启动 prompt |
| `status` | String | `"running"` / `"completed"` / `"failed"` / `"stopped"` |
| `session_id` | String | Claude session ID |
| `config_dir` | String | Claude config dir |
| `pid` | Integer | 子进程 PID |
| `created_at` | DateTime | 创建时间 |
| `completed_at` | DateTime | 完成时间 |
| `result` | Text | 最终结果（Sub-Agent 用 `submit_result` 写入） |

### 现有表结构（`sub_agent_reports`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | Integer PK | 自增 ID |
| `session_id` | Integer FK | 关联 sub_agent_sessions |
| `summary` | Text | 进度摘要 / 状态报告 |
| `is_important` | Boolean | Monitor 用；Sub-Agent 始终为 False |
| `created_at` | DateTime | 上报时间 |

Sub-Agent 的进度上报复用 `sub_agent_reports` 表，`is_important` 对 Sub-Agent 无意义（进度始终以 system_event 展示，结果始终注入主 session），固定为 `False`。

---

## MCP 工具定义

### 主 Agent 侧（`ccm_skills_server.py` 新增）

#### `create_sub_agent`

```python
@mcp.tool()
async def create_sub_agent(
    name: str,          # 子 Agent 名称，如 "security-reviewer"
    prompt: str,        # 任务 prompt，如 "审查 src/ 下所有文件的 SQL 注入风险"
    context: str = "",  # 额外上下文（可选，附加到 prompt 前）
) -> str:
    """
    创建一个 Sub-Agent 执行一次性任务。Sub-Agent 是独立的 Claude 子进程，
    会自主完成任务并将结果返回给你。你可以继续工作，稍后用 check_sub_agents 查看进度。

    返回: 创建成功的确认信息，包含 session_id
    """
    # 内部行为：
    # 1. HTTP POST /api/tasks/{task_id}/sub-agent-sessions
    #    body: {name, prompt, context, agent_type: "sub_agent"}
    # 2. 后端创建 DB 记录 + 启动子进程
    # 3. 返回 "Sub-Agent '{name}' (#{session_id}) 已创建，正在执行任务。"
```

#### `check_sub_agents`

```python
@mcp.tool()
async def check_sub_agents() -> str:
    """
    查看当前所有 Sub-Agent 的状态、进度和结果。

    返回: 格式化的状态列表，包含每个 Sub-Agent 的:
    - 名称和 ID
    - 状态 (running/completed/failed/stopped)
    - 最新进度摘要（如果 running）
    - 最终结果（如果 completed）
    """
    # 内部行为：
    # 1. HTTP GET /api/tasks/{task_id}/sub-agent-sessions?agent_type=sub_agent
    # 2. 包含每个 session 的最新 report 和 result
    # 3. 格式化为人类可读文本返回
```

#### `stop_sub_agent`

```python
@mcp.tool()
async def stop_sub_agent(
    session_id: int,    # 要停止的 Sub-Agent session ID
    reason: str = "",   # 停止原因（可选）
) -> str:
    """
    停止一个正在运行的 Sub-Agent。

    返回: 停止确认信息
    """
    # 内部行为：
    # 1. HTTP DELETE /api/tasks/{task_id}/sub-agent-sessions/{session_id}
    # 2. 后端 SIGTERM 子进程 → 等 10s → SIGKILL
    # 3. 标记 status="stopped"
    # 4. 返回确认信息
```

### Sub-Agent 侧（`ccm_sub_agent_server.py` 新建）

#### `report_progress`

```python
@mcp.tool()
async def report_progress(
    summary: str,  # 当前进度摘要，如 "已审查 15/42 个文件，发现 2 个高危问题"
) -> str:
    """
    向主 session 汇报当前进度。进度信息会实时显示在聊天界面中。

    返回: 确认信息
    """
    # 内部行为：
    # 1. HTTP POST /api/tasks/{task_id}/sub-agent-sessions/{session_id}/checks
    #    body: {summary, is_important: false}
    # 2. 写入 sub_agent_reports 表
    # 3. WS 广播 sub_agent_progress 事件到 task:{task_id} 频道
    # 4. 前端在聊天流中插入 system_event [Sub-Agent #{id}] 卡片
```

#### `submit_result`

```python
@mcp.tool()
async def submit_result(
    result: str,                    # 最终结果（Markdown 格式）
    status: str = "completed",      # "completed" 或 "failed"
) -> str:
    """
    提交最终结果并结束 Sub-Agent。结果会注入主 session 唤醒主 Agent。
    调用此工具后，Sub-Agent 进程将自动终止。

    返回: 确认信息（Sub-Agent 进程随后退出）
    """
    # 内部行为：
    # 1. HTTP POST /api/tasks/{task_id}/sub-agent-sessions/{session_id}/complete
    #    body: {result, status}
    # 2. 更新 DB：status, result, completed_at
    # 3. enqueue_message 注入主 session：
    #    "[Sub-Agent: {name}] 任务完成\n\n{result}"
    # 4. WS 广播 sub_agent_completed 事件
    # 5. 返回确认 → Sub-Agent 进程退出（自行 exit）
```

#### `get_context`

```python
@mcp.tool()
async def get_context() -> str:
    """
    获取任务上下文，包括项目信息、task 描述、主 Agent 的对话摘要等。

    返回: 格式化的上下文信息
    """
    # 内部行为：
    # 1. HTTP GET /api/tasks/{task_id}/sub-agent-sessions/{session_id}/context
    # 2. 返回：
    #    - task.description + task.prompt
    #    - project.name + project.local_path（如有）
    #    - 主 session 最近 N 条消息摘要
    #    - Sub-Agent 自身的 prompt 和 context
```

---

## API 端点

### 新增端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/tasks/{task_id}/sub-agent-sessions` | 创建 Sub-Agent session + 启动子进程 |
| `GET` | `/api/tasks/{task_id}/sub-agent-sessions` | 列出所有子 agent sessions（支持 `?agent_type=sub_agent` 过滤） |
| `DELETE` | `/api/tasks/{task_id}/sub-agent-sessions/{session_id}` | 停止 Sub-Agent |
| `POST` | `/api/tasks/{task_id}/sub-agent-sessions/{session_id}/checks` | 上报进度（复用 Monitor check 端点） |
| `POST` | `/api/tasks/{task_id}/sub-agent-sessions/{session_id}/complete` | 提交结果并标记完成 |
| `GET` | `/api/tasks/{task_id}/sub-agent-sessions/{session_id}/context` | 获取任务上下文 |

**说明**：大部分端点复用现有 Monitor 的 `sub_agent_sessions` API（`backend/api/monitor.py` 或独立 `backend/api/sub_agents.py`），仅需扩展 `agent_type` 过滤和 `complete` 端点的结果注入逻辑。

### 请求/响应示例

**创建 Sub-Agent**：
```json
// POST /api/tasks/42/sub-agent-sessions
{
  "name": "security-reviewer",
  "prompt": "审查 backend/api/ 下所有端点的认证和授权逻辑，列出缺少鉴权的端点",
  "context": "项目使用 Bearer token 认证，中间件在 backend/middleware/auth.py",
  "agent_type": "sub_agent"
}

// Response 201
{
  "id": 7,
  "task_id": 42,
  "agent_type": "sub_agent",
  "name": "security-reviewer",
  "status": "running",
  "created_at": "2026-07-07T10:00:00Z"
}
```

**提交结果**：
```json
// POST /api/tasks/42/sub-agent-sessions/7/complete
{
  "result": "## 安全审查报告\n\n### 缺少鉴权的端点\n1. GET /api/system/stats — 暴露实例数量\n...",
  "status": "completed"
}

// Response 200
{
  "id": 7,
  "status": "completed",
  "completed_at": "2026-07-07T10:05:00Z"
}
```

---

## 生命周期图

```
主 Agent                        Sub-Agent 子进程                   系统
   │                                                                │
   │  create_sub_agent(name, prompt)                                │
   │──────────────────────────────────────────────────────────────►  │
   │                                │  POST /sub-agent-sessions     │
   │                                │◄──────────────────────────────│
   │                                │                               │
   │                                │  创建 DB 记录                  │
   │                                │  status = "running"           │
   │                                │                               │
   │                          ┌─────┴─────┐                         │
   │                          │  启动子进程 │                         │
   │                          │  claude -p │                         │
   │                          └─────┬─────┘                         │
   │                                │                               │
   │  （主 Agent 继续其他工作）       │  report_progress("25% 完成")  │
   │                                │─────────────────────────────► │
   │                                │                    WS: sub_agent_progress
   │                                │               前端: [Sub-Agent #7] 25% 完成
   │                                │                               │
   │                                │  report_progress("75% 完成")  │
   │                                │─────────────────────────────► │
   │                                │                    WS: sub_agent_progress
   │                                │                               │
   │                                │  submit_result(report, "completed")
   │                                │─────────────────────────────► │
   │                                │                               │
   │                                │           ┌───────────────────┤
   │                                │           │ 更新 DB:           │
   │                                │           │  status=completed │
   │                                │           │  result=report    │
   │                                │           │  completed_at=now │
   │                                │           │                   │
   │                                │           │ enqueue_message:  │
   │◄───────────────────────────────│───────────│ [Sub-Agent] 结果  │
   │  user_message:                 │           │                   │
   │  "[Sub-Agent: security-       │           │ WS: sub_agent_    │
   │   reviewer] 任务完成\n..."     │           │  completed        │
   │                                │           └───────────────────┤
   │                          ┌─────┴─────┐                         │
   │                          │  进程退出   │                         │
   │                          └───────────┘                         │
   │                                                                │
   │  主 Agent 收到结果，继续处理                                      │
   │  "根据安全审查报告，我来修复..."                                   │
   ▼                                                                ▼

异常路径：
   │                                │  进程异常退出 (exit_code != 0) │
   │                                │─────────────────────────────► │
   │                                │           ┌───────────────────┤
   │                                │           │ status="failed"   │
   │◄───────────────────────────────│───────────│ enqueue_message:  │
   │  "[Sub-Agent: xxx] 执行失败:   │           │ 失败通知           │
   │   exit_code=1, 请检查日志"     │           └───────────────────┤
   │                                                                │

超时路径：
   │                                │  运行超过 2 小时               │
   │                                │                    ┌──────────┤
   │                                │◄───────────────────│ SIGTERM  │
   │                                │                    │ 等 10s   │
   │                                │                    │ SIGKILL  │
   │◄───────────────────────────────│────────────────────│ 通知     │
   │  "[Sub-Agent: xxx] 执行超时    │                    └──────────┤
   │   (2h)，已强制停止"            │                               │
   ▼                                                                ▼
```

---

## 消息显示规则

### 聊天流中的 Sub-Agent 消息

| 事件 | 聊天流展示 | 样式 |
|------|-----------|------|
| `sub_agent_progress` | `system_event` 卡片：`[Sub-Agent #7: security-reviewer] 已审查 15/42 个文件` | 灰色卡片，左侧蓝色竖线 |
| `sub_agent_completed` | 不直接插入（由 `user_message` 注入覆盖） | — |
| `user_message`（source=sub_agent） | 用户气泡：`[Sub-Agent: security-reviewer] 任务完成\n\n## 安全审查报告\n...` | 蓝紫色左边框，区分人工消息 |
| Sub-Agent 失败 | `user_message` 注入：`[Sub-Agent: xxx] 执行失败: ...` | 红色左边框 |

### 与 Monitor 消息去重方案的一致性

Sub-Agent 的消息展示复用 Monitor 消息去重设计（见 `monitor-message-dedup-design.md`）：

- `report_progress`：仅 `sub_agent_progress` 事件 → 聊天流 system_event（不注入主 session，不会触发主 Agent 回复）
- `submit_result`：仅 `user_message` 注入主 session → 唤醒主 Agent。`sub_agent_completed` 事件只更新前端状态面板，不插入聊天流

---

## 禁用 Native Agent

在 `instance_manager.py` 启动子进程时，当 task 启用了 Sub-Agent skill，自动添加 `--disallowedTools` 参数：

```python
# instance_manager.py 中构建 CLI 参数时
if task.enabled_skills and task.enabled_skills.get("sub_agent"):
    # 禁用 Native Agent/Task 工具，强制走 CCM Sub-Agent 插件
    cmd.extend(["--disallowedTools", "Agent,Task"])
```

这样主 Agent 只能通过 CCM 的 `create_sub_agent` MCP 工具创建子 Agent，确保所有子 Agent 都在 CCM 管控下。

---

## 并发控制

每 task 最多 **3 个** 并发 Sub-Agent（Monitor 上限为 5 个，二者独立计数）。

```python
# backend/api/sub_agents.py 或 monitor.py 中
MAX_SUB_AGENTS_PER_TASK = 3

async def create_sub_agent_session(task_id: int, ...):
    # 查询当前 running 的 sub_agent 数量
    running_count = await db.scalar(
        select(func.count(SubAgentSession.id)).where(
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.status == "running",
        )
    )
    if running_count >= MAX_SUB_AGENTS_PER_TASK:
        raise HTTPException(
            status_code=429,
            detail=f"每个任务最多同时运行 {MAX_SUB_AGENTS_PER_TASK} 个 Sub-Agent"
        )
```

---

## 超时和错误处理

### 超时

- 默认超时：**2 小时**（Monitor 为 4 小时）
- 超时后：SIGTERM → 等 10s → SIGKILL
- 超时通知：`enqueue_message` 注入主 session，告知超时原因

```python
SUB_AGENT_TIMEOUT = 2 * 3600  # 2 小时

async def _sub_agent_watchdog(session_id: int, pid: int):
    """Sub-Agent 超时看门狗"""
    await asyncio.sleep(SUB_AGENT_TIMEOUT)
    # 检查是否仍在运行
    session = await get_session(session_id)
    if session and session.status == "running":
        # 超时终止
        await terminate_process(pid)
        session.status = "failed"
        session.result = f"执行超时（{SUB_AGENT_TIMEOUT // 3600}h），已强制停止"
        await db.commit()
        # 通知主 Agent
        await dispatcher.enqueue_message(
            session.task_id,
            f"[Sub-Agent: {session.name}] {session.result}"
        )
```

### 错误处理

| 场景 | 处理方式 |
|------|---------|
| 子进程 exit_code != 0 | 标记 `status="failed"`，注入失败消息到主 session |
| 子进程被 OOM Kill | 同上，额外记录 signal 信息 |
| MCP 通信失败 | Sub-Agent 内部重试 3 次，仍失败则 `submit_result(status="failed")` |
| 主 Task 被取消 | 级联停止所有 Sub-Agent（SIGTERM） |
| CCM 重启 | 孤儿 Sub-Agent 进程检测 + 清理（类似 Monitor 的孤儿回收） |

---

## 实施步骤

### Phase 1: Sub-Agent MCP Server

**目标**：创建 Sub-Agent 侧的 MCP Server，提供 `report_progress`、`submit_result`、`get_context` 三个工具。

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/mcp/ccm_sub_agent_server.py` | **新建** | FastMCP server，transport=stdio；三个工具通过 HTTP 调用 CCM 后端 API |
| `backend/services/mcp_config.py` | **修改** | `generate_sub_agent_mcp_config()` 新增 Sub-Agent 类型的 MCP 配置生成 |

### Phase 2: API 端点

**目标**：扩展现有 sub_agent_sessions API，支持 Sub-Agent 的创建、进度上报、结果提交。

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/api/sub_agents.py` | **修改** | 新增 `POST /complete` 端点（结果注入 + 状态更新）；新增 `GET /context` 端点；`POST /` 创建端点增加 `agent_type` 过滤和并发控制 |
| `backend/schemas/sub_agent.py` | **修改** | 新增 `SubAgentCreateRequest`（含 context 字段）、`SubAgentCompleteRequest`（含 result、status） |

### Phase 3: 子进程生命周期

**目标**：在 Dispatcher / InstanceManager 中实现 Sub-Agent 子进程的启动、监控、超时、清理。

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/services/dispatcher.py` | **修改** | 新增 `_launch_sub_agent()` 方法：构建 CLI 命令 + MCP config → 启动子进程 → 注册看门狗；`enqueue_message()` 新增 `sub_agent_session_id` 参数 |
| `backend/services/instance_manager.py` | **修改** | `launch()` 中检测 `enabled_skills.sub_agent` → 添加 `--disallowedTools Agent,Task` |

### Phase 4: 主 Agent MCP 工具

**目标**：在 `ccm_skills_server.py` 中实现 `create_sub_agent`、`check_sub_agents`、`stop_sub_agent`。

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/mcp/ccm_skills_server.py` | **修改** | 新增三个 MCP 工具，通过 HTTP 调用后端 API |

### Phase 5: 前端展示

**目标**：在 ChatView 中渲染 Sub-Agent 消息，在 TaskForm 中添加 Sub-Agent skill 开关。

| 文件 | 操作 | 说明 |
|------|------|------|
| `frontend/src/components/Chat/ChatView.tsx` | **修改** | 处理 `sub_agent_progress` 和 `sub_agent_completed` WebSocket 事件；渲染 `[Sub-Agent]` 标记的 system_event 和 user_message |
| `frontend/src/components/Chat/SubSessionIndicator.tsx` | **修改** | 统计时包含 `agent_type="sub_agent"` 的 session |
| `frontend/src/components/Tasks/TaskForm.tsx` | **修改** | `enabled_skills` 新增 `sub_agent` 开关 |
| `frontend/src/api/client.ts` | **修改** | 新增 Sub-Agent 相关 API 类型定义 |

### Phase 6: 测试与文档

**目标**：编写测试用例，更新 CLAUDE.md 和 TEST.md。

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/tests/test_sub_agent.py` | **新建** | 单元测试：创建、进度上报、结果提交、并发控制、超时 |
| `CLAUDE.md` | **修改** | 新增 Sub-Agent 插件相关约定 |
| `TEST.md` | **修改** | 新增 Sub-Agent 测试用例 |

---

## 改动文件清单

| 文件路径 | 操作 | Phase |
|---------|------|-------|
| `backend/mcp/ccm_sub_agent_server.py` | **新建** | 1 |
| `backend/services/mcp_config.py` | 修改 | 1 |
| `backend/api/sub_agents.py` | 修改 | 2 |
| `backend/schemas/sub_agent.py` | 修改 | 2 |
| `backend/services/dispatcher.py` | 修改 | 3 |
| `backend/services/instance_manager.py` | 修改 | 3 |
| `backend/mcp/ccm_skills_server.py` | 修改 | 4 |
| `frontend/src/components/Chat/ChatView.tsx` | 修改 | 5 |
| `frontend/src/components/Chat/SubSessionIndicator.tsx` | 修改 | 5 |
| `frontend/src/components/Tasks/TaskForm.tsx` | 修改 | 5 |
| `frontend/src/api/client.ts` | 修改 | 5 |
| `backend/tests/test_sub_agent.py` | **新建** | 6 |
| `CLAUDE.md` | 修改 | 6 |
| `TEST.md` | 修改 | 6 |
