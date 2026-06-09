# Monitor Sub-Agent 重构方案（多 Agent 系统第一步）

> **工作环境约束**
> - **CWD**: `/home/ubuntu/Claude-Code-Manager-dev`
> - **分支**: `dev`（从 dev 分支工作，禁止修改 main 分支）
> - **禁止修改**: `/home/ubuntu/Claude-Code-Manager`（生产环境，端口 8002）
> - **禁止操作**: `git checkout main`、`git push origin main`、修改生产目录下任何文件
> - **测试服务**: 端口 8003，域名 `test.claude-code-manager.com`
> - **测试 Token**: `123456`

---

## 设计目标

将 CCM 从单 Agent 系统改造为多 Agent 系统。Monitor 是第一个子 Agent 类型，但架构设计上要为后续子 Agent 类型（如 researcher、builder 等）预留通用抽象。

### 核心理念

- **Task/Chat = 主 Agent**：拥有工具（tools）、技能（skills）、命令（slash commands）
- **子 Agent**：由主 Agent 通过 skill 创建，持久运行，拥有自己的 MCP tools，自主决策，通过 API 与系统通信
- **通用子 Agent 生命周期**：注册 → 启动 → 运行（自主通信）→ 完成/停止 → 清理

### 架构图

```
Task (主 Agent Session)
  │
  ├─ Skills (MCP tools for main agent)
  │   ├─ create_monitor()     ──→ API ──→ 创建 + 启动子 Agent
  │   ├─ check_monitors()     ──→ API ──→ 读取子 Agent 报告
  │   └─ stop_monitor()       ──→ API ──→ 终止子 Agent
  │
  └─ Sub-Agents (持久 Claude 子进程)
      └─ Monitor Agent #1
          ├─ 自己的 MCP Server (ccm_monitor_agent_server.py)
          │   ├─ report_status(summary, is_important)
          │   ├─ mark_complete(reason)  ──→ 自行结束进程
          │   └─ get_context()
          ├─ Bash tool (只读: ps, tail, cat)
          └─ 通信路径: MCP tool → HTTP API → DB + WebSocket → 前端/主 Agent
```

---

## 实现步骤

### Phase 1: 子 Agent MCP Server

- [x] **1.1** 创建 `backend/mcp/ccm_monitor_agent_server.py`
  - FastMCP server，transport=stdio
  - 参数: `--monitor-session-id`, `--task-id`, `--api-base`, `--auth-token`
  - 模块级变量: `_MONITOR_SESSION_ID`, `_TASK_ID`, `_API_BASE`, `_AUTH_TOKEN`

- [x] **1.2** 实现 `report_status(summary: str, is_important: bool = False)` tool
  - POST `/api/tasks/{task_id}/monitor-sessions/{session_id}/checks`
  - 请求体: `{"summary": summary, "status": "success", "is_important": is_important}`
  - 返回确认 + checks_done 计数 + 剩余次数

- [x] **1.3** 实现 `mark_complete(reason: str)` tool
  - POST `/api/tasks/{task_id}/monitor-sessions/{session_id}/complete`
  - 返回: `"Session completed. Your task is done — stop all activity now."`
  - 子 agent 收到返回后自然结束对话，进程退出

- [x] **1.4** 实现 `get_context()` tool
  - GET `/api/tasks/{task_id}/monitor-sessions/{session_id}`
  - 返回: description, monitor_context, checks_done, max_checks, status

### Phase 2: 新增 API Endpoints

- [x] **2.1** POST `/{session_id}/checks` — 子 agent 报告状态
  - 在 `backend/api/monitor.py` 新增
  - Schema: `{"summary": str, "status": str, "is_important": bool}`
  - 创建 MonitorCheck 记录 + 更新 MonitorSession.checks_done/last_summary
  - WebSocket 广播 `monitor_check` 事件（复用现有事件格式）
  - checks_done >= max_checks 时自动标记 completed

- [x] **2.2** POST `/{session_id}/complete` — 子 agent 标记完成
  - Schema: `{"reason": str}`
  - 设 status=completed + 创建最终 MonitorCheck + WebSocket 广播
  - 同时通知 dispatcher 让它知道进程即将退出

### Phase 3: MCP Config 生成

- [ ] **3.1** 在 `backend/services/mcp_config.py` 新增 `generate_monitor_agent_mcp_config()`
  ```python
  def generate_monitor_agent_mcp_config(
      monitor_session_id: int, task_id: int, api_base: str | None = None
  ) -> Path:
  ```
  - 文件: `/tmp/ccm_monitor_agent_{monitor_session_id}.json`
  - server: `backend.mcp.ccm_monitor_agent_server`
  - args: `--monitor-session-id`, `--task-id`, `--api-base`, `--auth-token`

- [ ] **3.2** 新增 `cleanup_monitor_agent_mcp_config(monitor_session_id: int)`

### Phase 4: 重构 Dispatcher

- [ ] **4.1** 重写 `_monitor_session_lifecycle()`
  - 删除 while True 轮询循环
  - 新流程: 读 DB → 构建 prompt → 生成 MCP config → 启动持久子进程 → `wait_for(proc.wait(), timeout=MAX_HOURS)` → 检查 session 状态 → 清理
  - 超时兜底: 最大 4 小时，超时 kill
  - 进程退出后: 如果 session 仍为 running（子 agent 异常退出未调 mark_complete），标记 failed
  - 保留: CancelledError 处理（kill 进程）、异常处理（标记 failed）

- [ ] **4.2** 新方法 `_launch_monitor_agent()` 替代旧 `_run_monitor_subprocess()`
  - 构建 Claude CLI 命令（含 `--mcp-config`）
  - stdout 写入日志文件 `/tmp/ccm_monitor_{session_id}.log`（不用 PIPE 防阻塞）
  - `start_new_session=True` 隔离进程组
  - 返回 `asyncio.subprocess.Process`

- [ ] **4.3** 新方法 `_build_monitor_agent_prompt()` 替代旧 `_build_monitor_prompt()`
  - Agent 风格 prompt:
    ```
    你是一个自主监控 Agent，持续监控目标并在有变化时主动汇报。

    ## 监控目标
    {description}

    ## 上下文
    {context}

    ## 你的 MCP 工具
    - report_status(summary, is_important): 报告状态。重要变化设 is_important=True
    - mark_complete(reason): 监控目标完成时调用，然后立即停止所有活动
    - get_context(): 获取最新监控配置

    ## 行为准则
    1. 用 Bash 执行 ps、tail、cat 等命令检查状态
    2. 自主判断频率：初期密集，稳定后放宽
    3. 重要变化立即 report_status，平时间隔较长
    4. 任务完成/失败/异常 → mark_complete 并说明原因
    5. 你是只读观察者，不要修改任何文件
    6. 检查间用 sleep，不要无间断轮询
    7. 调用 mark_complete 后，你的工作就结束了，不要再做任何事

    先做一次初始状态检查，然后持续观察。
    ```

- [ ] **4.4** 删除旧的 `_build_monitor_prompt()` 和 `_run_monitor_subprocess()`

### Phase 5: 停止机制

- [ ] **5.1** 更新 `delete_monitor_session()` API
  - 现有逻辑不变: cancel asyncio task + kill process
  - 新增: `cleanup_monitor_agent_mcp_config(session_id)`

- [ ] **5.2** `_monitor_session_lifecycle` 的 finally 块
  - 清理 MCP config 文件
  - 清理日志文件（可选保留）
  - 清理进程引用

### Phase 6: 前端 — 工具权限按钮

Task 卡片上的可展开工具权限指示器。

- [ ] **6.1** 在 `TaskList.tsx` Row 1 badges 区域新增工具按钮
  - 条件: `t.enabled_skills` 有至少一个为 true 的 key
  - 外观: lucide `Wrench` 图标 + 数量，如 `🔧1`
  - 样式: `text-xs bg-amber-600/30 text-amber-300 px-1.5 rounded cursor-pointer hover:bg-amber-600/40`

- [ ] **6.2** 点击展开工具列表
  - `useState<number | null>` 跟踪展开状态
  - 展开区域在 Row 1 与 Row 2 之间，显示所有 skill 的 badge
  - 已启用: 绿色 badge `✓ Monitor`；未启用的不显示
  - 通用遍历 `Object.entries(enabled_skills)` 渲染

### Phase 7: 前端 — 子 Agent 计数徽章

- [ ] **7.1** 后端: 在 Task response schema 中新增 `active_sub_agents` 字段
  - 类型: `int`，默认 0
  - 在 `backend/api/tasks.py` 的 list_tasks 查询中，对每个 active task 子查询 `SELECT COUNT(*) FROM monitor_sessions WHERE task_id=? AND status='running'`
  - 用 SQLAlchemy subquery/lateral join 避免 N+1

- [ ] **7.2** 前端 `client.ts`: Task interface 新增 `active_sub_agents: number`

- [ ] **7.3** `TaskList.tsx` 新增子 agent 徽章
  - 条件: `t.active_sub_agents > 0`
  - 外观: lucide `Users` 图标 + 数量，加 `animate-pulse`
  - 样式: `text-xs bg-teal-600/30 text-teal-300 px-1.5 rounded`

- [ ] **7.4** 点击展开子 agent 详情
  - 需要新 API: `GET /api/tasks/{task_id}/sub-agents/summary`
  - 返回: `{"by_type": {"monitor": {"running": 1, "completed": 3}}}`
  - 在 `backend/api/sub_agents.py` 实现（新文件，为后续子 agent 类型预留）
  - 展开面板按类型显示: `Monitor  ● 1 running  ○ 3 completed`

- [ ] **7.5** 前端 `client.ts` 新增 `SubAgentSummary` interface 和 `getSubAgentSummary()` API

### Phase 8: 测试

- [ ] **8.1** 启动开发环境 `./start-dev.sh`（确认端口 8003，不影响 8002 生产）
- [ ] **8.2** 创建 task + 启用 monitor skill
- [ ] **8.3** API 创建 monitor session → 验证子 agent 进程启动
- [ ] **8.4** 验证 report_status MCP tool → DB MonitorCheck 记录 + WebSocket
- [ ] **8.5** 验证子 agent 自主调整检查频率
- [ ] **8.6** 验证 mark_complete → session completed + 子 agent 进程自行退出
- [ ] **8.7** 验证 stop_monitor → 进程被 kill + 状态更新
- [ ] **8.8** 验证主 Agent check_monitors 读取子 agent 报告
- [ ] **8.9** 前端: 工具按钮展开/收起
- [ ] **8.10** 前端: 子 agent 徽章计数正确，完成后更新
- [ ] **8.11** 更新 TEST.md

### Phase 9: 文档

- [ ] **9.1** 更新 CLAUDE.md
- [ ] **9.2** 更新 README.md
- [ ] **9.3** 提交到 dev 分支（`git add && git commit`，不 push main）

---

## 不变的部分

| 组件 | 说明 |
|------|------|
| 主 Agent MCP tools | `create_monitor`, `check_monitors`, `stop_monitor` 接口不变 |
| DB 模型 | `MonitorSession`, `MonitorCheck` 表结构不变 |
| MonitorPanel | Chat 内的监控面板不变，同一数据源 |
| MonitorSession 字段 | interval/max_checks 保留，作为子 agent 参考配置 |

## 关键文件清单

| 文件 | 操作 |
|------|------|
| `backend/mcp/ccm_monitor_agent_server.py` | **新建** — 子 agent MCP server |
| `backend/api/monitor.py` | 新增 2 个 endpoint (checks, complete) |
| `backend/api/sub_agents.py` | **新建** — 通用子 agent summary API |
| `backend/services/mcp_config.py` | 新增 2 个函数 |
| `backend/services/dispatcher.py` | 重写 3 个方法 |
| `backend/api/tasks.py` | Task response 新增 active_sub_agents 字段 |
| `frontend/src/api/client.ts` | 新增 SubAgentSummary + active_sub_agents |
| `frontend/src/components/Tasks/TaskList.tsx` | 新增工具按钮 + 子 agent 徽章 |

## 安全检查清单

- [ ] 所有操作在 `/home/ubuntu/Claude-Code-Manager-dev`（dev 分支）
- [ ] 不 checkout main、不 push main、不修改生产目录
- [ ] 测试服务使用 8003 端口、token=123456
- [ ] 新 API endpoint 使用现有 Bearer token 认证
