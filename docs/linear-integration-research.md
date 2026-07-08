# Linear 集成调研报告

> 调研日期：2026-07-08  
> 调研目的：评估 CCM 接入 Linear 项目管理平台的可行性、实现方案和收益

---

## 目录

1. [Linear 平台概述](#1-linear-平台概述)
2. [API 与技术能力](#2-api-与技术能力)
3. [CCM × Linear 概念映射](#3-ccm--linear-概念映射)
4. [集成架构设计](#4-集成架构设计)
5. [核心 Use Cases](#5-核心-use-cases)
6. [实现路线图](#6-实现路线图)
7. [收益与风险分析](#7-收益与风险分析)
8. [与现有集成的对比](#8-与现有集成的对比)
9. [结论与建议](#9-结论与建议)

---

## 1. Linear 平台概述

Linear 是一款面向工程团队的项目管理工具，以快速、简洁的交互体验著称，被大量科技公司采用。与 Jira 等传统工具相比，Linear 的 API 更现代（纯 GraphQL）、数据模型更清晰、Webhook 更可靠。

### 1.1 核心概念

| Linear 概念 | 说明 |
|-------------|------|
| **Workspace** | 顶层组织单位，一个公司对应一个 workspace |
| **Team** | 工作组，每个 team 有独立的状态流（workflow states） |
| **Issue** | 核心工作单元，包含标题、描述、优先级、状态、负责人等 |
| **Project** | 跨 team 的大型工作集合，可包含多个 issue |
| **Cycle** | Sprint/迭代，时间限定的工作周期 |
| **Label** | 标签，支持层级结构 |
| **Workflow State** | 自定义状态（默认：Backlog → Triage → Todo → In Progress → In Review → Done → Canceled） |

### 1.2 优先级体系

Linear 的优先级用数字表示，**数字越小优先级越高**——与 CCM 完全一致：

| Linear | CCM | 含义 |
|--------|-----|------|
| 0 | — | No priority |
| 1 (Urgent) | P0 | 紧急 |
| 2 (High) | P1 | 高优 |
| 3 (Medium) | P2 | 中等 |
| 4 (Low) | P3+ | 低优 |

这一天然对齐意味着优先级同步无需任何转换逻辑。

---

## 2. API 与技术能力

### 2.1 GraphQL API

Linear 提供纯 GraphQL API，端点为 `https://api.linear.app/graphql`。

**认证方式：**
- **Personal API Key**：适合内部工具/自托管场景。在 Linear Settings → API 中生成，请求头 `Authorization: <api-key>`
- **OAuth 2.0**：适合第三方 SaaS 集成。标准授权码流程，Scopes 包括 `read`、`write`、`issues:create`、`comments:create`、`admin`

**关键操作示例：**

```graphql
# 创建 Issue
mutation {
  issueCreate(input: {
    teamId: "TEAM_ID"
    title: "实现用户认证模块"
    description: "需要支持 JWT + OAuth2..."
    priority: 2
    labelIds: ["LABEL_ID"]
  }) {
    success
    issue { id identifier title url }
  }
}

# 查询 Issue（带过滤）
query {
  issues(filter: {
    state: { name: { eq: "Todo" } }
    priority: { lte: 2 }
  }) {
    nodes { id identifier title priority { label } state { name } }
  }
}

# 更新 Issue 状态
mutation {
  issueUpdate(id: "ISSUE_ID", input: {
    stateId: "DONE_STATE_ID"
  }) {
    success
    issue { id state { name } }
  }
}

# 添加评论
mutation {
  commentCreate(input: {
    issueId: "ISSUE_ID"
    body: "## 执行报告\n\n任务已完成，commit: abc123..."
  }) {
    success
    comment { id body }
  }
}
```

### 2.2 Webhook

Linear 提供完整的 Webhook 支持：

- **支持的事件**：Issue 创建/更新/删除、Comment 创建/更新、Project/Cycle/Label 变更
- **Payload 格式**：JSON，包含 `action`（create/update/remove）、`type`、`data`、`url`、`organizationId`
- **安全验签**：HMAC 签名验证，与 CCM 现有的 GitHub Webhook 验签模式一致
- **可过滤**：按 Team、Project、Label 过滤事件

**Webhook Payload 示例：**
```json
{
  "action": "create",
  "type": "Issue",
  "data": {
    "id": "abc-123",
    "identifier": "ENG-42",
    "title": "重构认证模块",
    "priority": 2,
    "state": { "name": "Todo" },
    "team": { "key": "ENG" },
    "description": "详细描述...",
    "labels": [{ "name": "backend" }]
  },
  "url": "https://linear.app/team/issue/ENG-42",
  "createdAt": "2026-07-08T10:00:00.000Z"
}
```

### 2.3 Rate Limits

- 1,500 requests/hour（API Key）
- 基于 complexity 的查询限制（单次最大 10,000 complexity）
- 游标分页，每页最多 250 条

对 CCM 场景足够——任务同步频率通常远低于此限制。

### 2.4 Python 生态

Linear 没有官方 Python SDK，但 GraphQL API 足够简单，两种方案：

| 方案 | 优点 | 缺点 |
|------|------|------|
| `httpx`（异步）直接发 GraphQL | 零依赖、与 CCM 现有技术栈一致 | 需手写 query 字符串 |
| `gql` 库 | 类型安全、自动补全 | 多一个依赖 |

**推荐用 `httpx`**：CCM 已在用 httpx/aiohttp，不引入新依赖。封装一个 `LinearClient` 类即可覆盖所有需求。

---

## 3. CCM × Linear 概念映射

### 3.1 实体映射

| Linear | CCM | 映射说明 |
|--------|-----|----------|
| **Issue** | **Task** | 核心 1:1 映射。Linear Issue = CCM Task |
| **Project** | **Project** | 直接映射。Linear Project ↔ CCM Project |
| **Priority** (1-4) | **Priority** (P0-P3) | 天然对齐，数字越小越高 |
| **Workflow State** | **Task Status** | 需映射表（见下方） |
| **Label** | **Tag** | CCM Task 有 `tags` JSON 字段 |
| **Comment** | **Chat Message** | Claude 的执行结果/对话可写回为 Linear Comment |
| **Assignee** | **Instance** | Linear 分配给人，CCM 分配给 Claude 实例 |
| **Cycle** | — | CCM 无迭代概念，可忽略或作为标签同步 |
| **Attachment** | — | Claude 的 PR link、日志链接可作为 attachment 写入 |

### 3.2 状态映射

```
Linear State          ←→    CCM Status          触发条件
─────────────────────────────────────────────────────────────
Backlog / Triage      ←→    (不同步)             仅同步 Todo 及以后
Todo                  →     pending              Webhook: Issue 移入 Todo
In Progress           ←     in_progress          CCM: 实例开始处理
In Review             ←     executing            CCM: Claude 正在执行
Done                  ←     completed            CCM: 任务完成
Canceled              ←→    cancelled            双向同步
```

**关键设计决策：不同步 Backlog 和 Triage**。这两个状态是人工规划阶段，只有当 Issue 被明确移入 Todo 后才需要 CCM 接管执行。这避免了 Claude 过早领取尚未 scope 好的任务。

### 3.3 数据模型扩展

Task 模型需新增字段：

```python
class Task(Base):
    # ... 现有字段 ...
    linear_issue_id: str | None      # Linear Issue UUID
    linear_issue_identifier: str | None  # 可读标识符，如 "ENG-42"
    linear_issue_url: str | None     # Linear Issue URL
    linear_sync_enabled: bool = True # 是否启用双向同步
```

新增配置模型：

```python
class LinearIntegration(Base):
    id: int
    team_id: str           # Linear Team ID
    team_key: str          # Team 标识符（如 "ENG"）
    project_id: int | None # 关联的 CCM Project
    api_key: str           # 加密存储的 API Key
    webhook_secret: str    # Webhook 验签密钥
    auto_import: bool      # 是否自动导入 Todo 状态的 Issue
    auto_sync_status: bool # 是否自动回写状态
    state_mapping: dict    # 自定义状态映射
```

---

## 4. 集成架构设计

### 4.1 整体架构

```
┌─────────────────┐              ┌─────────────────┐
│     Linear      │              │      CCM         │
│                 │    Webhook   │                  │
│  Issue Created  │────────────→│  /api/linear/    │
│  Issue Updated  │              │  webhook         │
│  Issue Deleted  │              │      │           │
│                 │              │      ▼           │
│                 │   GraphQL    │  LinearService   │
│  Status Update  │←────────────│      │           │
│  Comment Added  │              │      ▼           │
│  Attachment     │              │  Task CRUD       │
│                 │              │  Dispatcher      │
└─────────────────┘              └─────────────────┘
```

### 4.2 模块设计

```
backend/
├── api/
│   └── linear.py              # Webhook 端点 + 手动同步 API
├── models/
│   └── linear_integration.py  # LinearIntegration ORM 模型
├── schemas/
│   └── linear.py              # Pydantic 请求/响应模型
└── services/
    └── linear_service.py      # Linear API 客户端 + 同步逻辑

frontend/
└── src/
    ├── pages/
    │   └── LinearPage.tsx     # Linear 集成配置页面
    └── components/
        └── Linear/
            ├── LinearConfig.tsx   # Team/Project 映射配置
            └── LinearBadge.tsx    # Task 上的 Linear 标识
```

### 4.3 同步流程

**Linear → CCM（Webhook 驱动）：**

1. Linear Issue 状态变为 "Todo" → Webhook 触发
2. CCM 收到 Webhook → 验签 → 解析 Issue 数据
3. 按 `team_id` 查找 `LinearIntegration` 配置
4. 创建 CCM Task（映射 title/description/priority/tags）
5. Task 进入 pending 队列 → Dispatcher 正常调度

**CCM → Linear（事件驱动）：**

1. Task 状态变更 → Dispatcher 触发回调
2. `LinearService` 检查 task 是否关联 Linear Issue
3. 调用 GraphQL API 更新 Issue 状态
4. 任务完成时：添加 Comment（执行摘要 + commit 链接）、添加 Attachment（PR URL）

**冲突处理：**

- 乐观锁：每次同步带 `updatedAt` 时间戳，晚于 Linear 当前值才更新
- 单向优先：状态以 CCM 为准（CCM 是实际执行方），描述/标题以 Linear 为准（人工编辑方）
- 防循环：Webhook 处理时设置 `_sync_source = "linear"` 标记，回写时跳过 linear 触发的变更

### 4.4 参考现有模式

CCM 已有一个成熟的 Webhook 集成模式——**PR Monitor**（GitHub Webhook）。Linear 集成可以完全复用这套模式：

| 维度 | PR Monitor (GitHub) | Linear Integration |
|------|--------------------|--------------------|
| Webhook 端点 | `/api/github/webhook` | `/api/linear/webhook` |
| 验签 | HMAC-SHA256 | HMAC 签名 |
| 事件处理 | PR opened → 创建审核 Task | Issue → Todo → 创建 Task |
| 状态回写 | 审核通过 → merge PR | Task 完成 → Issue Done |
| 配置模型 | `MonitoredRepo` | `LinearIntegration` |
| WebSocket | `pr-monitor` channel | `linear` channel |

代码结构和处理流程可以直接参照 `backend/api/pr_monitor.py` + `backend/services/pr_review_service.py`。

---

## 5. 核心 Use Cases

### Use Case 1：Linear 看板驱动 Claude 开发

**场景**：团队在 Linear 规划迭代，将确定要做的 Issue 拖入 "Todo"，Claude 自动领取并执行。

**流程：**
```
PM 在 Linear 创建 Issue "ENG-42: 增加用户注册邮件验证"
    → 在 Description 中写好 PRD 和验收标准
    → 拖入 Todo
    → Webhook 触发 CCM 创建 Task
    → Claude 自动领取、创建 worktree、实现、提交、merge
    → Task 完成 → Linear Issue 自动变为 Done
    → Linear Comment 写入执行摘要和 commit 链接
```

**好处**：PM 不需要登录 CCM，在 Linear 就能发起和追踪 AI 开发任务。

### Use Case 2：执行进度实时同步

**场景**：Claude 在执行长任务时，团队成员可以在 Linear 看到实时状态。

**同步内容：**
- 状态变更 → Linear 状态更新
- Plan 审批请求 → Linear Comment（"等待审批：Claude 提出了以下方案..."）
- Ask User 问题 → Linear Comment + 通知（"Claude 在询问：数据库该用哪种索引策略？"）
- 执行完成 → Linear Comment（含 git diff 摘要、测试结果、PR 链接）

### Use Case 3：批量任务规划

**场景**：将一个 Linear Project 下的所有 Todo Issue 批量导入 CCM，按优先级排队执行。

```
Linear Project "Q3 Backend Refactor"
    ├── ENG-41: 重构认证中间件 (P1)
    ├── ENG-42: 用户注册邮件验证 (P2)
    ├── ENG-43: API 限流器 (P2)
    └── ENG-44: 日志格式统一 (P3)

→ CCM 一键导入 → 4 个 Task 按 P1→P2→P3 排队
→ 多实例并行执行
→ 全部完成后 Linear 看板全部变 Done
```

### Use Case 4：人机协作循环

**场景**：Claude 无法独立完成的任务，通过 Linear 形成人机协作循环。

```
Claude 执行 ENG-42 → 遇到需要人工决策的问题
    → CCM 在 Linear Issue 上添加 Comment: "需要确认：邮件模板用 HTML 还是纯文本？"
    → Linear 通知相关团队成员
    → 团队成员在 Linear 回复 Comment
    → Webhook 推送到 CCM → 通过 chat 传给 Claude
    → Claude 继续执行
```

### Use Case 5：Label 驱动的智能路由

**场景**：不同 Label 的 Issue 自动路由到不同的 CCM 配置。

```
Label 映射规则：
    "backend"   → Project: ccm-backend,  Model: claude-opus-4-6
    "frontend"  → Project: ccm-frontend, Model: claude-sonnet-5
    "bugfix"    → Priority +1 (提高一级), Mode: goal
    "research"  → Mode: goal, 不自动合并

ENG-42 [backend, bugfix] → 自动创建：
    Project: ccm-backend
    Priority: P1 (原 P2 因 bugfix 提升)
    Model: claude-opus-4-6
    Mode: goal
```

### Use Case 6：执行报告与统计

**场景**：在 Linear 中追踪 Claude 的执行效率和产出。

**通过 Attachment 和 Comment 丰富 Issue 信息：**
- 执行耗时、token 消耗
- Git diff 统计（+/- 行数）
- 测试结果（通过/失败）
- 代码审查意见（如 PR Monitor 接入后）

Linear 原生的统计功能（Cycle velocity、团队负荷、完成率）可以直接覆盖 AI agent 的工作量，让 PM 把 Claude 当作"团队成员"纳入迭代规划。

---

## 6. 实现路线图

### Phase 1：单向导入 + 状态回写（约 2-3 天）

**目标**：Linear Issue → CCM Task，执行完自动回写 Done

**实现清单：**
- [ ] `LinearClient`（`backend/services/linear_service.py`）：httpx 异步 GraphQL 封装
- [ ] `LinearIntegration` ORM 模型 + Alembic migration
- [ ] Webhook 端点 `/api/linear/webhook`（验签 + Issue 事件处理）
- [ ] Task 模型新增 `linear_issue_id` / `linear_issue_identifier` / `linear_issue_url`
- [ ] Dispatcher 回调：状态变更时调用 LinearClient 更新 Issue State
- [ ] 任务完成时：向 Issue 添加执行摘要 Comment
- [ ] 前端：Task 卡片显示 Linear 标识和链接

### Phase 2：深度双向同步（约 2-3 天）

**目标**：Comment 双向同步、Label 映射、批量导入

**实现清单：**
- [ ] Comment Webhook 处理 → CCM chat 消息转发
- [ ] CCM chat/plan/ask-user 事件 → Linear Comment
- [ ] Label → Tag 映射 + 智能路由规则
- [ ] 手动批量导入 API：`POST /api/linear/import`（按 Project/Team 导入）
- [ ] 前端 LinearPage：配置 Team 映射、Label 规则、同步选项

### Phase 3：高级功能（约 1-2 天）

**目标**：统计、Cycle 集成、完整前端管理

**实现清单：**
- [ ] 执行报告 → Linear Attachment（含 diff stats、token cost）
- [ ] Cycle 感知：只导入当前活跃 Cycle 的 Issue
- [ ] 前端 Dashboard Linear 面板：同步状态、最近导入、失败告警
- [ ] OAuth 2.0 认证流程（如果需要多用户场景）

### 工作量估算

| Phase | 后端 | 前端 | 测试 | 总计 |
|-------|------|------|------|------|
| Phase 1 | 1.5 天 | 0.5 天 | 0.5 天 | ~2.5 天 |
| Phase 2 | 1.5 天 | 1 天 | 0.5 天 | ~3 天 |
| Phase 3 | 1 天 | 0.5 天 | 0.5 天 | ~2 天 |
| **总计** | **4 天** | **2 天** | **1.5 天** | **~7.5 天** |

---

## 7. 收益与风险分析

### 7.1 收益

| 维度 | 收益 | 影响 |
|------|------|------|
| **用户体验** | PM/团队成员无需登录 CCM 即可发起和追踪 AI 任务 | 大幅降低使用门槛 |
| **工作流集成** | AI 开发成为团队正常迭代流程的一部分 | 从"工具"升级为"工作方式" |
| **可见性** | Linear 的看板/时间线/统计覆盖 AI 产出 | 让 AI 工作量可度量、可规划 |
| **协作** | 通过 Linear Comment 实现人机协作闭环 | 不中断团队现有工作流 |
| **扩展性** | Linear 的 Slack/邮件通知生态自动生效 | 无需在 CCM 侧实现通知 |
| **产品差异化** | 市面上几乎没有 AI 编码平台与 Linear 深度集成 | 竞争优势 |

### 7.2 风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| API Rate Limit（1500/h） | 低 | 中 | 批量操作合并请求、缓存状态 |
| Webhook 丢失 | 低 | 中 | 定期轮询对账 + 手动同步按钮 |
| 状态同步冲突 | 中 | 低 | 乐观锁 + 单向优先规则 |
| Linear API 变更 | 低 | 中 | GraphQL schema 有版本保证，破坏性变更罕见 |
| 额外复杂度 | — | 中 | Phase 1 先做最小可用版本验证价值 |

### 7.3 可行性判断：完全可行

1. **技术成熟度高**：Linear GraphQL API 文档完善，认证简单，Webhook 可靠
2. **概念天然对齐**：Priority 数字一致、Issue ↔ Task 1:1、Label ↔ Tag 直接映射
3. **现有参照模式**：PR Monitor 已经验证了 Webhook 集成模式在 CCM 中的可行性
4. **工作量可控**：Phase 1 最小可用版本 2-3 天，价值验证成本低
5. **无新依赖**：httpx 已在项目中使用，无需引入额外库

---

## 8. 与现有集成的对比

### 8.1 CCM 现有外部集成

| 集成 | 方向 | 机制 | 复杂度 |
|------|------|------|--------|
| **GitHub PR Monitor** | GitHub → CCM → GitHub | Webhook + API | 中 |
| **Claude Pool** | CCM → Claude API | CLI 子进程 | 高 |
| **Cloudflare Tunnel** | CCM → 公网 | 系统进程 | 低 |

### 8.2 Linear 集成的定位

Linear 集成是 CCM 的**第一个"上游任务源"集成**。PR Monitor 是"事件驱动的审核任务创建"，而 Linear 集成是"任务管理平台驱动的开发任务创建"——后者更通用，覆盖面更广。

它打通的是 **"谁来决定做什么"** 的问题：
- 没有 Linear：任务只能在 CCM Web UI 中手动创建
- 有了 Linear：PM 在 Linear 规划 → 拖入 Todo → Claude 自动执行 → Linear 看板实时更新

### 8.3 与 Jira / GitHub Issues 的比较

| 维度 | Linear | Jira | GitHub Issues |
|------|--------|------|---------------|
| API 类型 | GraphQL（现代） | REST v2/v3（复杂） | REST + GraphQL |
| 认证 | API Key / OAuth2 | OAuth 2.0 (3LO)（复杂） | Personal Token / GitHub App |
| Webhook | 原生、简洁 | 原生但 payload 庞大 | 原生 |
| Python SDK | 无官方（但 GraphQL 简单） | `jira` 库（成熟） | `PyGithub` / `ghapi` |
| 优先级模型 | 数字 1-4（与 CCM 对齐） | 自定义 scheme | 无内置优先级 |
| 状态管理 | Team 级自定义 | 项目级自定义 | Open / Closed（极简） |
| 集成复杂度 | **低** | **高** | **中** |
| 目标用户 | 工程团队 | 企业通用 | 开源/小团队 |

**Linear 是 CCM 的最佳首选集成目标**：API 简洁、概念对齐、目标用户匹配、实现成本最低。如果后续需要，可以基于相同的架构模式扩展 Jira / GitHub Issues 集成。

---

## 9. 结论与建议

### 9.1 核心结论

**Linear 集成完全可行，且性价比极高。** CCM 的任务模型与 Linear 的 Issue 模型天然对齐（优先级、状态、标签），现有的 Webhook 集成模式可以直接复用，技术风险低，工作量可控。

### 9.2 建议

1. **推荐接入**。Linear 集成解决了 CCM 从"开发者工具"到"团队工作流平台"的关键一步
2. **从 Phase 1 开始验证**。2-3 天实现最小可用版本（Issue 导入 + 状态回写），在实际使用中验证价值
3. **复用 PR Monitor 模式**。Webhook 端点、验签、事件处理、WebSocket 广播的代码结构可以直接参照
4. **API Key 认证即可**。自托管场景无需 OAuth 复杂度，Phase 1 用 Personal API Key 最简单
5. **先做单 Team 映射**。一个 Linear Team 对应一个 CCM Project，验证通了再支持多 Team

### 9.3 已知的 AI 编码工具 + Linear 集成先例

- **SweepAI**：开源 AI 编码 agent，支持从 Linear Issue 自动生成 PR
- **Devin**：可以被指派 Linear Issue 并自主开发
- **Lindy.ai**：AI agent 平台，支持 Linear Issue 自动分类和路由

CCM 的差异化在于：它不只是单次执行 Issue，而是提供**完整的多实例调度、session 管理、人机交互循环**，这些是其他工具所不具备的。Linear 集成让这些能力通过标准化的项目管理界面暴露出来。
