# Team CCM 设计方案

> 团队协作版 CCM：支持多人对同一项目/任务协作，通过飞书集成实现消息同步和身份绑定。
>
> 基于现有 Worker relay 架构设计，复用已有基础设施。

## 1. 系统概述

### 1.1 核心思路

现有 CCM 是单人使用的。Team CCM 在此基础上增加：

1. **Team CCM (Hub)**：团队中心节点，管理小组、项目、执行 task
2. **成员 CCM (Member)**：个人节点，接收团队项目/任务，可查看和交互
3. **飞书集成**：每个 task 可绑定飞书群，实现 CCM ↔ 飞书双向消息同步

### 1.2 与 Worker 模式的关系

| | Worker 模式 | Team 模式 |
|---|---|---|
| 连接方向 | Manager → Worker | Member → Hub |
| 执行位置 | Worker 上执行 | Hub（+Hub 的 Worker）上执行 |
| 远端角色 | 无头执行节点 | 完整 CCM，有独立 UI 和个人 task |
| 数据同步 | Worker relay（WS）推事件到 Manager | Hub 通过 Member 主动建立的 WS 推事件 |
| 凭证持有 | Manager 持有 Worker token | Hub 不持有 Member 任何凭证 |

### 1.3 角色配置

同一套代码，通过配置区分角色：

```bash
# Team CCM (Hub) 的 .env
TEAM_MODE=hub
TEAM_NAME=AI-Lab

# 成员 CCM (Member) 的 .env
TEAM_MODE=member
# team_hub_connection 表存储 Hub 地址和 token（注册时自动写入）

# 普通 CCM（不参与团队）
# 不配置 TEAM_MODE
```

- `hub`：启用小组管理、项目分发、成员注册 API；前端 Team 页面显示管理视图
- `member`：连接 Hub 接收团队数据；前端 Team 页面显示团队项目/任务
- 不配置：无 Team 功能，和现有 CCM 一致

Team CCM 也可以配置自己的 Worker（和现有 Worker 系统完全一致）。

## 2. 团队功能

### 2.1 小组管理（Hub 侧）

- 创建小组、编辑小组信息
- 生成邀请码供成员加入
- 查看小组成员列表和在线状态
- 小组成员通过飞书 OAuth 绑定身份

### 2.2 项目归属

**归属单位是 Project，不是 Task**：

- Hub 管理员创建 Project 时，指定归属小组（`team_id`）
- 该 Project 及其下所有 Task 自动同步到小组成员的 CCM
- 成员可以在 Team 页面查看 project 下的 task、发消息、创建新 task
- 成员**不可以**创建新 project（只有 Hub 管理员有权）
- 成员**可以**修改 project 配置

### 2.3 Task 执行位置

**所有 task 都在 Hub 本机 + Hub 的 Worker 上执行。** 成员 CCM 只是查看和交互的窗口，不执行 task。成员看到的是 Hub 事件的镜像。

### 2.4 成员权限

| 操作 | 是否允许 |
|------|---------|
| 查看团队 project/task | 可以 |
| 在 task 中发消息 | 可以 |
| 创建 project 下的新 task | 可以 |
| 停止/取消执行中的 task | 可以 |
| 重试失败的 task | 可以 |
| 修改 project 配置 | 可以 |
| 看到同组其他成员发的消息 | 可以 |
| 创建新 project | 不可以 |
| 删除 project | 不可以 |

### 2.5 前端 Team 页面

新增页面，位于 Workers 和 PR Monitor 旁边。

| 身份 | Team 页面内容 |
|------|-------------|
| Hub | 小组管理 + 成员列表 + 团队 Project/Task 全局视图 |
| Member | 团队分发的 Project 列表 + 其下 Task（类似 Tasks 页面，可发消息、可建 task、不可建 project） |

**前端路由逻辑**：
- Tasks 页面：`projects WHERE hub_project_id IS NULL`（个人项目）
- Team 页面：`projects WHERE hub_project_id IS NOT NULL`（团队项目）

两个页面共用同一个 ChatView 组件，发消息的路径不同：
- 个人 task：`POST /api/tasks/{id}/chat` → 本地 session
- 团队 task：`POST /api/tasks/{id}/chat` → 检测到 `hub_task_id` → 代理转发到 Hub API

## 3. 通信机制

### 3.1 连接方向

**Member 主动连 Hub**（与 Worker relay 方向相反）：

```
成员 CCM ──WebSocket──→ Hub CCM     （接收事件推送）
成员 CCM ──HTTP POST──→ Hub CCM     （发消息/建 task/操作）
```

Hub **不需要访问成员 CCM**，不持有成员的任何凭证。事件推送通过成员主动建立的 WebSocket 连接完成。

选择此方向的原因：
- 成员 CCM 是个人服务器，Hub 不应持有其凭证（隐私保护）
- 成员可能在 NAT 后面（虽然目前都有公网，但不依赖此假设更稳健）
- Hub 签发 token 给成员（权限可控），而非成员把 token 交给 Hub

### 3.2 事件推送

Hub 通过 WebSocket 推送给 Member 的事件（与 Worker relay 的 `CHAT_EVENT_TYPES` 基本一致）：

| 事件类别 | 事件类型 | 说明 |
|---------|---------|------|
| 项目同步 | project_created, project_updated, project_deleted | 团队 project 变更 |
| Task 同步 | task_created, status_change, context_usage | task 生命周期 |
| Chat 事件 | message, result, tool_use, tool_result, thinking, system_init | 复用现有 CHAT_EVENT_TYPES |
| 飞书绑定 | feishu_chat_bound | task 绑定飞书群后通知成员 |

成员 CCM 收到事件后写入本地 `projects`/`tasks`/`log_entries` 表（与 Worker relay 双写 LogEntry 的逻辑一致）。

### 3.3 断线重连与补发

成员 CCM 离线期间，Hub 暂存事件。重连后：
1. 成员 WebSocket 重新连接（指数退避重试，与 Worker relay `_reconnect` 一致）
2. Hub 补发缺失的事件（与 Worker relay `_backfill_missing_logs` 一致）
3. 同步 task 状态（可能在离线期间变更）

### 3.4 成员操作代理

成员在 Team 页面的所有写操作都通过 Member 后端代理到 Hub：

```
Member 前端 → Member API → Hub API → Hub 执行 → 事件推回 Member WebSocket
```

Member 后端做代理（前端只和自己的后端通信），避免前端直接暴露 Hub 地址和 token。

代理的操作包括：
- 发送聊天消息：`POST hub/api/tasks/{hub_task_id}/chat`
- 创建 task：`POST hub/api/tasks`（带 project_id）
- 停止 task：`POST hub/api/tasks/{hub_task_id}/stop-session`
- 取消 task：`POST hub/api/tasks/{hub_task_id}/cancel`
- 重试 task：`POST hub/api/tasks/{hub_task_id}/retry`
- 修改 project：`PUT hub/api/projects/{hub_project_id}`

## 4. 成员注册流程

### 4.1 完整流程

```
1. Hub 管理员在 Team 页面创建小组
   → Hub 生成邀请码（如 "ABC123"，可设过期时间）

2. 成员在自己 CCM 的设置页面完成飞书 OAuth 绑定
   → 成员 CCM 本地 feishu_user_binding 表存下 feishu_open_id

3. 成员在 Team 页面点"加入团队"
   → 输入 Hub 地址 + 邀请码 → 点击"加入"

4. 成员 CCM 后端自动执行：
   a. 读取本地 feishu_user_binding 拿到 feishu_open_id/feishu_name
   b. POST hub_url/api/team/register
      body: { invite_code, feishu_open_id, feishu_name }
   c. Hub 验证邀请码 → 创建 team_member 记录 → 返回:
      { member_token, team_name, team_id, member_id }
   d. 自动写入 team_hub_connection 表
   e. 自动建立 WebSocket 长连接到 Hub
   f. Hub 推送该成员所属小组的 project/task 数据
   g. 自动写入本地 projects/tasks/log_entries

5. 成员 Team 页面立刻显示团队内容
```

**用户感知**：输入地址和邀请码 → 点加入 → Team 页面出现团队内容。全自动。

### 4.2 邀请码管理

- Hub 管理员可为每个小组生成邀请码
- 邀请码可设置：过期时间、最大使用次数
- 已使用的邀请码可查看关联的成员
- 管理员可随时作废邀请码

## 5. 数据模型

### 5.1 Hub 侧新增表

```sql
-- 小组
CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 小组成员
CREATE TABLE team_members (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    feishu_open_id VARCHAR(100),          -- 飞书身份
    member_token VARCHAR(200) NOT NULL,    -- Hub 签发给成员的 token
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- active / inactive
    last_seen_at DATETIME,                 -- 最后 WebSocket 活跃时间
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(team_id, feishu_open_id)
);

-- 邀请码
CREATE TABLE team_invites (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    code VARCHAR(50) NOT NULL UNIQUE,
    max_uses INTEGER,                      -- NULL = 无限
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at DATETIME,                   -- NULL = 永不过期
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Hub 侧现有表扩展：

```sql
-- projects 表新增字段
ALTER TABLE projects ADD COLUMN team_id INTEGER REFERENCES teams(id);
-- team_id 非空 = 团队项目，NULL = Hub 自己的项目
```

`tasks` 表不改——task 通过 `project_id → project.team_id` 确定归属小组。

### 5.2 Member 侧新增表

不建平行表，在现有表上加来源标记：

```sql
-- 现有 projects 表新增字段
ALTER TABLE projects ADD COLUMN hub_project_id INTEGER;
ALTER TABLE projects ADD COLUMN hub_url VARCHAR(500);
-- hub_project_id 非空 = 从 Hub 同步来的团队项目

-- 现有 tasks 表新增字段
ALTER TABLE tasks ADD COLUMN hub_task_id INTEGER;
-- hub_task_id 非空 = 从 Hub 同步来的团队 task
-- 发消息时不走本地 session，而是代理到 Hub API
```

`log_entries` 表不改——Hub 通过 WebSocket 推送事件，Member 侧直接写入 `log_entries`（`instance_id=NULL`，与 Worker relay 一致）。

Member 侧新增连接信息表：

```sql
-- Hub 连接信息
CREATE TABLE team_hub_connection (
    id INTEGER PRIMARY KEY,
    hub_url VARCHAR(500) NOT NULL,
    hub_token VARCHAR(200) NOT NULL,       -- Hub 签发的 member_token
    member_id INTEGER NOT NULL,            -- 在 Hub 的 team_members.id
    team_name VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'connected',  -- connected / disconnected
    last_sync_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 5.3 飞书相关表（Hub 和 Member 共用）

```sql
-- Task ↔ 飞书群映射
CREATE TABLE feishu_chat_bindings (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE,       -- 一个 task 对应一个群
    chat_id VARCHAR(100) NOT NULL UNIQUE,  -- 飞书群 ID
    chat_name VARCHAR(200),
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- active / archived
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 飞书用户绑定（本 CCM 实例的所有者）
CREATE TABLE feishu_user_binding (
    id INTEGER PRIMARY KEY,
    feishu_open_id VARCHAR(100) NOT NULL UNIQUE,
    feishu_name VARCHAR(100),
    bound_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

飞书应用凭证走 `.env` 配置：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

每个 CCM 实例（Hub 和 Member）各自创建自己的飞书应用。

### 5.4 数据关系图

```
Hub 侧:
  teams ──→ team_members (飞书 open_id + member_token)
    │
    └──→ team_invites (邀请码)

  projects (team_id) ──→ tasks ──→ log_entries
                           │
                           └── feishu_chat_bindings

Member 侧:
  team_hub_connection (hub_url + hub_token)

  projects (hub_project_id, hub_url) ──→ tasks (hub_task_id) ──→ log_entries
                                           │
                                           └── feishu_chat_bindings

  feishu_user_binding (本机所有者的飞书身份)
```

## 6. 飞书集成

### 6.1 Bot 配置

- 每个 CCM 实例各自一个飞书应用（一对 app_id/app_secret）
- 各自通过 WebSocket 长连接接收自己 Bot 的事件（参考 agent-ml-research 的 feishu_webhook.py）
- **谁的 task，谁的 Bot 管群**

### 6.2 身份绑定

通过飞书 OAuth 完成：
1. CCM 设置页面提供"绑定飞书"按钮
2. 跳转飞书 OAuth 授权页面
3. 回调后获取 open_id 和用户名
4. 写入 `feishu_user_binding` 表

### 6.3 建群策略

| 场景 | 建群时机 | Bot 归属 |
|------|---------|---------|
| 团队 task | task 创建时自动建群，拉入归属小组所有成员 | Team CCM 的 Bot |
| 个人 task | 用户在 CCM 上点"绑定飞书"按需建群 | 个人 CCM 的 Bot |

### 6.4 消息双向同步

```
飞书 → CCM:
  用户在飞书群发消息
  → Bot WebSocket 收到事件（带 chat_id + sender open_id）
  → 查 feishu_chat_bindings: chat_id → task_id
  → 注入到对应 task session（复用 pty-bridge 注入机制）
  → 消息前缀标记发送者: "[飞书用户 张三] 消息内容"

CCM → 飞书:
  Claude 回复 → assistant message 事件
  → 查 feishu_chat_bindings: task_id → chat_id
  → Bot API 发送到对应飞书群
  → 支持 Markdown 格式、代码块、长文本自动截断
```

### 6.5 群归档

task 完成后：
- 飞书群标记为归档（不解散，保留历史消息）
- `feishu_chat_bindings.status` 设为 `archived`
- 归档的群不再接收新消息注入

## 7. 完整数据流

### 7.1 Hub 创建团队项目

```
Hub 管理员创建 Project(team_id=1)
  │
  ├─ 查 team_members WHERE team_id=1 → [成员A, 成员B, 成员C]
  │
  ├─ 通过各成员的 WebSocket 连接推送 project_created 事件
  │   payload: { project_id, name, git_url, ... }
  │
  └─ 各成员 CCM 收到 → 写入本地 projects(hub_project_id=X, hub_url=...)
```

### 7.2 Hub 创建并执行 Task

```
Hub 创建 Task(project_id=P, project.team_id=1)
  │
  ├─ 飞书自动建群，拉入小组成员
  │   → 写入 feishu_chat_bindings
  │
  ├─ 推送 task_created 事件到成员 WebSocket
  │   payload: { task_id, project_id, description, feishu_chat_id, ... }
  │
  ├─ Hub Dispatcher 执行 task（本机或 Worker）
  │
  └─ 执行过程中持续推送 chat 事件到成员 WebSocket
      → 成员 CCM 写入 log_entries → 前端实时刷新
      → 同时通过飞书 Bot 发到群里
```

### 7.3 成员发送消息

```
成员在 Team 页面的 ChatView 输入消息
  │
  ├─ 前端 POST /api/tasks/{local_task_id}/chat
  │
  ├─ Member 后端检测到 hub_task_id 非空
  │   → 代理 POST hub_url/api/tasks/{hub_task_id}/chat
  │     headers: { Authorization: Bearer member_token }
  │     body: { message, sender_name, sender_feishu_id }
  │
  ├─ Hub 收到 → 注入到 task session
  │   → Claude 回复 → 事件广播
  │
  └─ Hub 推送事件到所有小组成员的 WebSocket
      → 所有成员（包括发送者）看到回复
      → 飞书群也收到回复
```

### 7.4 成员创建 Task

```
成员在 Team 页面某 project 下点"新建 Task"
  │
  ├─ 前端 POST /api/team/tasks
  │   body: { hub_project_id, description, ... }
  │
  ├─ Member 后端代理到 Hub
  │   → POST hub_url/api/tasks
  │     body: { project_id: hub_project_id, description, ... }
  │
  ├─ Hub 创建 task → 自动建飞书群 → 开始执行
  │
  └─ 事件推送到所有小组成员
```

## 8. 后端服务新增

### 8.1 Hub 侧新增服务

| 服务 | 文件 | 说明 |
|------|------|------|
| TeamManager | `services/team_manager.py` | 小组 CRUD、成员管理、邀请码 |
| TeamRelay | `services/team_relay.py` | 管理成员 WebSocket 连接、推送事件 |
| FeishuBot | `services/feishu_bot.py` | 飞书 Bot：建群、发消息、WebSocket 收消息 |

### 8.2 Member 侧新增服务

| 服务 | 文件 | 说明 |
|------|------|------|
| TeamHubClient | `services/team_hub_client.py` | 连接 Hub WebSocket、接收事件、写入本地 DB |
| TeamProxy | `services/team_proxy.py` | 代理成员操作到 Hub API |

### 8.3 共用服务

| 服务 | 文件 | 说明 |
|------|------|------|
| FeishuClient | `services/feishu_client.py` | 飞书 SDK 封装：OAuth、建群、发消息、WebSocket 事件 |

## 9. API 端点

### 9.1 Hub 侧 API

```
# 小组管理
POST   /api/teams                          创建小组
GET    /api/teams                          列出小组
GET    /api/teams/{id}                     小组详情 + 成员列表
PUT    /api/teams/{id}                     更新小组
DELETE /api/teams/{id}                     删除小组

# 成员管理
POST   /api/teams/{id}/invites             生成邀请码
GET    /api/teams/{id}/invites             列出邀请码
DELETE /api/teams/{id}/invites/{code}      作废邀请码

# 成员注册（成员 CCM 调用）
POST   /api/team/register                  注册成员（验证邀请码，返回 member_token）

# WebSocket（成员 CCM 连接）
WS     /ws/team?token={member_token}       成员事件推送通道
```

### 9.2 Member 侧 API

```
# 加入团队
POST   /api/team/join                      加入团队（输入 hub_url + invite_code）
GET    /api/team/status                    连接状态
DELETE /api/team/leave                     退出团队

# 团队操作代理（前端调用，后端转发到 Hub）
POST   /api/team/tasks                     创建团队 task
POST   /api/team/tasks/{id}/chat           发送消息
POST   /api/team/tasks/{id}/stop           停止 task
POST   /api/team/tasks/{id}/cancel         取消 task
POST   /api/team/tasks/{id}/retry          重试 task
PUT    /api/team/projects/{id}             修改 project 配置
```

### 9.3 飞书 API（Hub 和 Member 共用）

```
POST   /api/feishu/bind                    飞书 OAuth 绑定
DELETE /api/feishu/bind                    解除绑定
GET    /api/feishu/status                  绑定状态
POST   /api/tasks/{id}/feishu/bind         为 task 创建飞书群（个人 task 按需）
DELETE /api/tasks/{id}/feishu/bind         解除 task 飞书群绑定
```

## 10. 前端页面

### 10.1 Team 页面 — Hub 视图

```
┌─────────────────────────────────────────────┐
│  Team: AI-Lab                               │
├──────────┬──────────────────────────────────┤
│ 小组列表  │  小组详情                         │
│          │                                  │
│ > 前端组  │  成员：                           │
│   后端组  │    张三 (在线) feishu: zhang3     │
│   AI组   │    李四 (离线) feishu: li4        │
│          │                                  │
│ [+新建]  │  邀请码：ABC123 (剩余3次)         │
│          │  [生成新邀请码]                    │
│          │                                  │
│          │  项目：                           │
│          │    web-app (12 tasks)             │
│          │    api-server (5 tasks)           │
│          │  [+关联项目到此小组]               │
├──────────┴──────────────────────────────────┤
│  团队 Task 全局视图（所有小组的 task）         │
│  类似现有 Tasks 页面，多显示"归属小组"列       │
└─────────────────────────────────────────────┘
```

### 10.2 Team 页面 — Member 视图

```
┌─────────────────────────────────────────────┐
│  Team: AI-Lab (已连接)                       │
├──────────┬──────────────────────────────────┤
│ 团队项目  │  Task 列表 / ChatView            │
│          │                                  │
│ web-app  │  和现有 Tasks 页面布局一致         │
│ api-srv  │  可发消息、创建 task、停止/重试    │
│          │  不可创建新 project               │
│          │                                  │
│          │  多一个飞书群标识（已绑群/未绑群）  │
└──────────┴──────────────────────────────────┘
```

## 11. 实施计划

### Phase 1：团队基础（Hub 侧）
- teams / team_members / team_invites 表 + API
- projects.team_id 字段
- Team 页面 Hub 视图（小组管理 + 成员列表）
- 邀请码生成和验证

### Phase 2：成员连接
- 成员注册流程（POST /api/team/register）
- team_hub_connection 表
- Hub 侧 TeamRelay：管理成员 WebSocket 连接
- Member 侧 TeamHubClient：连接 Hub、接收事件、写入本地 DB
- projects.hub_project_id / tasks.hub_task_id 字段
- Team 页面 Member 视图（团队项目/任务列表）

### Phase 3：成员交互
- Member 侧 TeamProxy：代理操作到 Hub API
- ChatView 适配团队 task（发消息走 Hub 代理）
- 成员创建 task、停止/取消/重试
- 多成员消息可见性（所有成员看到所有人的消息）

### Phase 4：飞书集成
- FeishuClient：SDK 封装（OAuth、建群、发消息、WebSocket 事件接收）
- 飞书 OAuth 绑定流程
- 团队 task 自动建群 + 拉入小组成员
- 个人 task 按需建群
- 消息双向同步（飞书 ↔ CCM）
- 已完成 task 群归档

### Phase 5：完善
- 断线重连 + 事件补发
- 成员在线状态显示
- 团队 task 全局搜索/过滤
- 权限细化（只读成员 vs 可操作成员）
- 飞书消息格式优化（Markdown、代码块、长文本截断）

## 12. 关键设计决策总结

| 决策 | 选择 | 理由 |
|------|------|------|
| 角色区分 | 同代码 + 配置区分 | 部署简单，不维护多个代码库 |
| 归属单位 | Project 归属小组 | 比 task 级别更自然，一个项目的所有 task 都归同一组 |
| 连接方向 | Member 连 Hub | Hub 不持有成员凭证，保护隐私 |
| Task 执行位置 | Hub + Hub 的 Worker | 成员只查看/交互，不执行 |
| 数据存储 | 复用现有表 + 加字段 | 复用 ChatView/WebSocket 等基础设施，不维护平行表 |
| 成员注册 | 邀请码 + 飞书 OAuth | 安全且用户友好 |
| 飞书 Bot | 每个 CCM 各自一个 | 消息隔离干净，不需要路由分发 |
| 飞书建群 | 团队 task 自动，个人按需 | 平衡通知及时性和群数量 |
| 飞书事件接收 | WebSocket 长连接 | 不需要公网回调地址，每个 CCM 独立 |
| 成员操作 | Member 后端代理 | 前端只和自己后端通信，简单一致 |
