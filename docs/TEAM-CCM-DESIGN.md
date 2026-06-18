# Team CCM 设计方案 v2

> P2P 任务分享 + 飞书身份绑定，让多人可以对同一个 task 进行协作对话。
>
> v2 重写：去掉中心 Hub 节点和飞书消息映射，改为轻量的 P2P 任务分享模式。

## 1. 核心思路

每个 CCM 都是平等的个人节点，没有中心 Hub。核心功能：

1. **飞书身份绑定**：CCM 绑定飞书账号，发现同一组织内的其他 CCM 用户
2. **Task 分享**：将自己的 task 分享给组织内其他成员，被分享者可以查看和对话
3. **代理模式**：被分享的 task 数据始终在分享者机器上，被分享者的 CCM 代理请求

**不做的事情**：
- 没有中心 Hub 节点
- 飞书不做消息转发/建群，只做身份绑定 + 分享通知
- 不同步数据到被分享者的 DB

## 2. 飞书集成

### 2.1 飞书应用

整个组织共用**一个飞书企业自建应用**（在飞书开放平台创建一次）：
- 所有 CCM 实例共用同一个 `app_id` / `app_secret`
- 每个 CCM 有自己的 `redirect_uri`（飞书应用支持配置多个重定向地址）
- 凭证通过 `.env` 配置：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

需要的飞书权限：
- `contact:user.employee_id:readonly`（读取组织成员列表）
- 网页登录能力（OAuth）
- 消息发送能力（分享通知用）

### 2.2 绑定流程

```
1. 用户在 CCM 设置页点"绑定飞书"

2. 前端跳转飞书授权页：
   https://open.feishu.cn/open-apis/authen/v1/authorize
     ?app_id=cli_xxx
     &redirect_uri=https://my-ccm.com/api/feishu/callback
     &state=random_state

3. 用户在飞书页面登录并授权

4. 飞书回调 CCM：
   GET /api/feishu/callback?code=xxx&state=random_state

5. CCM 后端用 code 换 user_access_token：
   POST https://open.feishu.cn/open-apis/authen/v1/oidc/access_token

6. 用 access_token 获取用户信息：
   GET https://open.feishu.cn/open-apis/authen/v1/user_info
   → 拿到 open_id、name、email、avatar_url

7. 写入本地 feishu_user_binding 表

8. 向组织注册表上报：
   POST {ORG_REGISTRY_URL}/api/org/register
   body: { open_id, name, ccm_url, avatar_url }
```

### 2.3 飞书的作用范围

| 用途 | 说明 |
|------|------|
| 身份绑定 | CCM 用户 ↔ 飞书 open_id |
| 组织发现 | 通过注册表查看组织内其他 CCM 用户 |
| 分享通知 | 分享/取消分享 task 时通过飞书 DM 通知对方 |

**不做**：不建群、不转发消息、不做消息双向同步。

## 3. 组织注册表

### 3.1 设计

P2P 架构没有中心节点，但成员发现需要一个共同的查询点。方案：**指定一个 CCM 兼任注册表**。

```bash
# 注册表所有者的 .env
ORG_REGISTRY_ENABLED=true

# 其他成员的 .env
ORG_REGISTRY_URL=https://youchengsong.claude-code-manager.com
```

注册表就是两个额外的 API 端点 + 一张表，不需要额外部署服务。

只有注册过 CCM（即绑定飞书并上报了 URL）的成员，在分享时才会被显示出来。

### 3.2 注册表 API

```
POST   /api/org/register              注册/更新自己的信息
GET    /api/org/members               获取组织内所有已注册成员
DELETE /api/org/members/{open_id}      注销成员
POST   /api/org/transfer              移交注册表到另一个 CCM
POST   /api/org/import                接收移交过来的注册表数据
POST   /api/org/registry-changed      接收注册表地址变更通知
```

### 3.3 注册表数据

```sql
-- 仅在注册表所有者的 CCM 上有数据
CREATE TABLE org_members (
    id INTEGER PRIMARY KEY,
    feishu_open_id VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    ccm_url VARCHAR(500) NOT NULL,
    avatar_url VARCHAR(500),
    registered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME
);
```

### 3.4 注册表转移

注册表所有者可以将注册表移交给组织内其他成员：

```
1. 当前所有者（A）在设置页点"移交注册表" → 选择新所有者（B）

2. A 的 CCM 推送注册表数据到 B：
   POST B/api/org/import
   body: { members: [...] }

3. B 的 CCM 存下数据，启用 ORG_REGISTRY_ENABLED

4. A 的 CCM 通知所有其他成员（注册表里有每个人的 ccm_url）：
   POST 每个成员/api/org/registry-changed
   body: { new_registry_url: "https://b.xxx.com" }

5. 每个成员的 CCM 自动更新 ORG_REGISTRY_URL

6. A 清除自己的 ORG_REGISTRY_ENABLED，变回普通成员
```

用户感知：点"移交" → 选人 → 完成。全自动。

## 4. Task 分享

### 4.1 分享流程

```
1. 分享者在 task 页面点"分享" → 弹出组织成员列表（从注册表获取）

2. 多选要分享的成员 → 点确认

3. 分享者 CCM 为每个被分享者：
   a. 生成 share_token（限定该 task + 只读+对话权限）
   b. 在本地 task_shares 表记录分享关系
   c. 推送分享信息到被分享者 CCM：
      POST 被分享者/api/shared/receive
      body: { 
        owner_ccm_url, task_id, share_token,
        task_title, task_description,
        owner_name, owner_feishu_id
      }
   d. 通过飞书 DM 通知被分享者：
      "张三分享了一个 task 给你：{task_title}
       点击查看：https://被分享者ccm.com/shared/{id}"

4. 被分享者 CCM 收到后：
   a. 存入本地 shared_tasks_received 表
   b. 前端 Shares 区域立即显示
```

### 4.2 被分享者查看 Task

**代理模式**：被分享者 CCM 不存 task 数据，实时从分享者 CCM 拿。

```
被分享者打开共享 task
  │
  ├─ 前端请求 GET /api/shared/{id}/history
  │
  ├─ 被分享者 CCM 后端代理：
  │   GET {owner_ccm_url}/api/shared-access/{task_id}/history
  │   headers: { Authorization: Bearer {share_token} }
  │
  ├─ 返回聊天历史 → 前端渲染
  │
  └─ 前端直连分享者 CCM 的 WebSocket 订阅实时事件：
     WS {owner_ccm_url}/ws/shared?token={share_token}&task_id={task_id}
     → 实时收到 Claude 回复、工具调用等事件
     → 关闭页面时断开（不是永久连接）
```

实时事件采用**前端直连**分享者 CCM 的 WebSocket（不经过被分享者 CCM 代理），更简单高效。share_token 限定只能访问对应 task，安全可控。

### 4.3 被分享者发消息

```
被分享者在共享 task 的 ChatView 输入消息
  │
  ├─ 前端 POST /api/shared/{id}/chat
  │   body: { message: "用户消息" }
  │
  ├─ 被分享者 CCM 后端代理：
  │   POST {owner_ccm_url}/api/shared-access/{task_id}/chat
  │   headers: { Authorization: Bearer {share_token} }
  │   body: { message: "[飞书用户 李四] 用户消息" }
  │
  └─ 分享者 CCM 处理消息 → Claude 回复
     → 通过 WebSocket 推送到所有在线的被分享者
```

消息前缀 `[飞书用户 xxx]` 让分享者和 Claude 能区分消息来源。分享者自己发的消息不加前缀。

### 4.4 多人同时对话

多个人同时给同一个 task 发消息时，逻辑和单人使用完全一致：
- 先发的先执行
- 后发的进入 pending queue
- task 本身不感知是多人在操作
- 分享者 CCM 的现有 `WebSocketBroadcaster` 按 `task:{id}` channel 广播，所有连着的 shared WS 连接都能收到事件

### 4.5 被分享者权限

| 操作 | 是否允许 |
|------|---------|
| 查看 task 聊天历史 | 可以 |
| 发送消息 | 可以 |
| 查看 config/模式（只读） | 可以 |
| 修改 task 配置/模式 | 不可以 |
| 停止/取消/重试 task | 不可以 |
| 创建新 task | 不可以 |
| 分享给其他人 | 不可以（只有 task 所有者可以） |

### 4.6 管理分享

**分享者可以随时增删被分享者**：

增加成员：
```
分享者点"管理分享" → 添加新成员
  → 生成 share_token → 写入 task_shares
  → 推送到新成员 CCM
  → 飞书 DM 通知新成员
```

删除成员：
```
分享者点"管理分享" → 移除成员
  → task_shares.status = "revoked"
  → 通知被分享者 CCM：POST /api/shared/revoke
  → 飞书 DM 通知被分享者："张三取消了 task 的分享"
  → 被分享者 CCM 删除 shared_tasks_received 记录
  → share_token 失效，后续请求被拒
```

### 4.7 分享者 CCM 离线

被分享者打开共享 task 时，如果分享者 CCM 不可达，显示文字提示："分享者 CCM 不可达，请稍后再试"。不做数据缓存。

## 5. 前端

### 5.1 Shares 按钮

在 Tasks 页面的 Filter / Projects 栏旁边新增 **Shares** 按钮：

```
┌──────────────────────────────────────────────┐
│  🔽 Filter    📁 Projects  ▼    🔍   Shares │
├──────────────────────────────────────────────┤
│  正常 task 列表...                            │
└──────────────────────────────────────────────┘
```

点击 Shares 按钮后，切换到共享 task 视图：

```
┌──────────────────────────────────────────────┐
│  🔽 Filter    📁 Projects  ▼    🔍   Shares │
├──────────────────────────────────────────────┤
│  📎 调研 VibeThinker        来自 张三         │
│  📎 部署 NanoChat 环境      来自 李四         │
│  📎 CI/CD 流水线调试        来自 王五         │
└──────────────────────────────────────────────┘
```

再次点击 Shares 按钮回到正常 task 视图。

### 5.2 共享 Task 列表项

和普通 task 显示一样，额外标注：
- 分享标记图标（📎 或其他）
- "来自 xxx"（分享者名字）

### 5.3 共享 Task 的 ChatView

和普通 ChatView 布局一致，区别：
- Config 按钮点开后内容**只读**（显示全部配置，但按钮灰掉/禁用）
- 没有停止/取消/重试按钮
- 输入框提示文字："以共享成员身份发送消息"
- 特殊 URL：`/shared/{shared_task_id}`

### 5.4 分享管理弹窗

Task 详情页新增"分享"按钮（分享者视角）：

```
┌─ Task: 调研 VibeThinker-3B ──────────────────────┐
│  [Chat] [Config] [分享 👥2]                       │
│                                                   │
│  ChatView 聊天内容...                              │
└───────────────────────────────────────────────────┘

点击"分享 👥2"弹窗：
┌─ 分享管理 ────────────────────────────┐
│  已分享给：                            │
│    ☑ 李四 (li4@company.com)  [移除]    │
│    ☑ 王五 (wang5@company.com) [移除]   │
│                                       │
│  添加成员：                            │
│    ☐ 张三 (zhang3@company.com)         │
│    ☐ 赵六 (zhao6@company.com)          │
│                                       │
│  [确认]  [取消]                        │
└───────────────────────────────────────┘
```

## 6. 数据模型

### 6.1 所有 CCM 共有

```sql
-- 飞书用户绑定（本 CCM 的所有者）
CREATE TABLE feishu_user_binding (
    id INTEGER PRIMARY KEY,
    feishu_open_id VARCHAR(100) NOT NULL UNIQUE,
    feishu_name VARCHAR(100),
    avatar_url VARCHAR(500),
    access_token TEXT,               -- 飞书 user_access_token（发消息用）
    token_expires_at DATETIME,
    bound_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 6.2 注册表所有者额外的表

```sql
-- 组织成员注册表（仅注册表所有者有数据）
CREATE TABLE org_members (
    id INTEGER PRIMARY KEY,
    feishu_open_id VARCHAR(100) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    ccm_url VARCHAR(500) NOT NULL,
    avatar_url VARCHAR(500),
    registered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME
);
```

### 6.3 分享者侧

```sql
-- 我分享出去的 task（分享者 CCM 上）
CREATE TABLE task_shares (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    shared_to_open_id VARCHAR(100) NOT NULL,   -- 被分享者飞书 ID
    shared_to_name VARCHAR(100),
    shared_to_ccm_url VARCHAR(500) NOT NULL,
    share_token VARCHAR(200) NOT NULL UNIQUE,   -- 访问令牌
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- active / revoked
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, shared_to_open_id)
);
```

### 6.4 被分享者侧

```sql
-- 别人分享给我的 task（被分享者 CCM 上）
CREATE TABLE shared_tasks_received (
    id INTEGER PRIMARY KEY,
    owner_ccm_url VARCHAR(500) NOT NULL,
    owner_name VARCHAR(100),
    owner_feishu_open_id VARCHAR(100),
    remote_task_id INTEGER NOT NULL,            -- 分享者 CCM 上的 task_id
    share_token VARCHAR(200) NOT NULL,
    task_title VARCHAR(200),
    task_description TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- active / revoked
    received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_ccm_url, remote_task_id)
);
```

### 6.5 数据关系图

```
分享者 CCM:
  feishu_user_binding (自己的飞书身份)
  tasks ──→ task_shares (分享给了谁，share_token)
  org_members (仅注册表所有者)

被分享者 CCM:
  feishu_user_binding (自己的飞书身份)
  shared_tasks_received (别人分享给我的，包含 owner_url + token)

请求流:
  被分享者前端 ─HTTP─→ 被分享者 CCM API ─代理─→ 分享者 CCM API (share_token)
  被分享者前端 ─WS──→ 分享者 CCM WebSocket (share_token, 直连)
```

## 7. 认证与安全

### 7.1 share_token 设计

- 分享者为每个 (task_id, 被分享者) 组合生成唯一的 share_token
- token 是随机字符串（`secrets.token_urlsafe(32)`）
- 在分享者 CCM 侧验证：收到请求后查 `task_shares` 表，检查 token 有效且 status=active
- token 权限限定：只能访问对应的 task，只能查看 + 发消息

### 7.2 分享者 CCM 的请求验证

```python
async def verify_share_token(token: str, task_id: int) -> TaskShare:
    share = await db.query(TaskShare).filter(
        TaskShare.share_token == token,
        TaskShare.task_id == task_id,
        TaskShare.status == "active",
    ).first()
    if not share:
        raise HTTPException(403, "Invalid or revoked share token")
    return share
```

### 7.3 分享者 CCM 新增的访问端点

```
# 被分享者通过 share_token 访问（不需要 admin auth_token）
GET    /api/shared-access/{task_id}/history?token={share_token}
POST   /api/shared-access/{task_id}/chat?token={share_token}
GET    /api/shared-access/{task_id}/config?token={share_token}
WS     /ws/shared?token={share_token}&task_id={task_id}
```

这些端点和现有的 `/api/tasks/{id}/chat` 功能相同，但认证方式不同（share_token 而非 admin auth_token），且权限受限（只读 config、只能发消息）。

## 8. 后端服务

### 8.1 新增服务

| 服务 | 文件 | 说明 |
|------|------|------|
| FeishuAuth | `services/feishu_auth.py` | 飞书 OAuth 绑定：授权跳转、回调处理、token 刷新 |
| OrgRegistry | `services/org_registry.py` | 组织注册表：成员注册/查询/转移 |
| TaskSharing | `services/task_sharing.py` | Task 分享：创建/撤销分享、token 生成、推送通知 |
| SharedProxy | `services/shared_proxy.py` | 共享代理：代理被分享者的请求到分享者 CCM |
| FeishuNotify | `services/feishu_notify.py` | 飞书通知：发送分享/撤销 DM 消息 |

### 8.2 新增 API 端点

```
# 飞书绑定
GET    /api/feishu/auth-url              获取飞书授权 URL
GET    /api/feishu/callback              飞书 OAuth 回调
GET    /api/feishu/status                绑定状态
DELETE /api/feishu/unbind                解除绑定

# 组织注册表（注册表所有者的端点）
POST   /api/org/register                注册/更新自己
GET    /api/org/members                 获取组织成员列表
DELETE /api/org/members/{open_id}        注销成员
POST   /api/org/transfer                移交注册表
POST   /api/org/import                  接收移交数据
POST   /api/org/registry-changed        接收注册表地址变更通知

# Task 分享（分享者侧）
POST   /api/tasks/{id}/share            分享 task 给成员（多选）
DELETE /api/tasks/{id}/share/{open_id}  取消分享
GET    /api/tasks/{id}/shares           查看该 task 的分享列表

# 分享者为被分享者提供的访问端点（share_token 认证）
GET    /api/shared-access/{task_id}/history  聊天历史
POST   /api/shared-access/{task_id}/chat    发送消息
GET    /api/shared-access/{task_id}/config  查看配置（只读）
WS     /ws/shared?token={share_token}&task_id={task_id}  实时事件（前端直连）

# 共享 task（被分享者侧）
POST   /api/shared/receive              接收分享推送
POST   /api/shared/revoke               接收撤销通知
GET    /api/shared/tasks                列出所有共享给我的 task
GET    /api/shared/{id}/history         代理获取聊天历史
POST   /api/shared/{id}/chat            代理发送消息
GET    /api/shared/{id}/config          代理获取配置（只读）
DELETE /api/shared/{id}                 主动退出共享
```

## 9. 完整数据流

### 9.1 飞书绑定 + 注册

```
用户点"绑定飞书"
  → 浏览器跳转飞书 OAuth → 授权 → 回调
  → CCM 拿到 open_id + name + avatar
  → 写入 feishu_user_binding
  → POST {ORG_REGISTRY_URL}/api/org/register
  → 注册表记录该用户的 ccm_url
  → 分享功能解锁
```

### 9.2 分享 Task

```
分享者点"分享" → 弹窗显示组织成员（GET /api/org/members）
  → 多选成员 → 确认
  → 为每个成员：
     1. 生成 share_token → 写入 task_shares
     2. POST 被分享者ccm/api/shared/receive（推送分享信息）
     3. 飞书 DM 通知被分享者（含链接）
  → 被分享者 CCM 收到 → 写入 shared_tasks_received
  → 被分享者前端 Shares 区域出现新 task
```

### 9.3 被分享者查看和对话

```
被分享者点击共享 task
  → GET /api/shared/{id}/history
    → 被分享者 CCM 代理 → GET 分享者ccm/api/shared-access/{task_id}/history
    → 返回聊天历史
  → 前端直连分享者 CCM WebSocket
    → WS 分享者ccm/ws/shared?token=xxx&task_id=xxx
    → 实时事件推送（关闭页面时断开）

被分享者发消息
  → POST /api/shared/{id}/chat
    → 被分享者 CCM 代理 → POST 分享者ccm/api/shared-access/{task_id}/chat
    → 消息前缀 "[飞书用户 李四]"
    → 分享者 CCM 注入 session → Claude 回复
    → WebSocket 推送到所有在线被分享者
```

### 9.4 取消分享

```
分享者移除成员
  → task_shares.status = "revoked"
  → POST 被分享者ccm/api/shared/revoke
  → 飞书 DM 通知
  → 被分享者 shared_tasks_received 删除
  → share_token 失效
```

## 10. 配置项

```bash
# 飞书应用（整个组织共用一个应用的凭证）
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx

# 组织注册表
ORG_REGISTRY_URL=https://youchengsong.claude-code-manager.com  # 指向注册表所有者
ORG_REGISTRY_ENABLED=true  # 仅注册表所有者设 true
```

## 11. 实施计划

### Phase 1：飞书绑定 + 组织注册表
- feishu_user_binding 表 + OAuth 绑定流程
- org_members 表 + 注册/查询 API
- 注册表转移功能
- 设置页面飞书绑定 UI

### Phase 2：Task 分享
- task_shares 表 + shared_tasks_received 表
- 分享/撤销分享 API
- 分享者：share_token 生成、分享管理弹窗 UI
- 被分享者：接收分享、Shares 按钮 + 共享 task 列表 UI
- 飞书 DM 通知（分享/撤销）

### Phase 3：共享 Task 交互
- 分享者侧：shared-access API（share_token 认证的聊天历史/发消息/配置）
- 分享者侧：shared WebSocket 端点（实时事件，前端直连）
- 被分享者侧：SharedProxy 代理 HTTP 请求
- 被分享者侧：共享 ChatView（只读 config、无停止按钮、可发消息）

### Phase 4：完善
- 分享者 CCM 离线提示
- 分享 task 的状态变更通知（task 完成/失败时飞书通知被分享者）
- 注册表心跳（定期检测成员 CCM 是否在线）
- 共享 task 搜索/过滤

### Phase 5：飞书群消息创建 Task（后续）
- 在飞书群里添加 CCM Bot
- 用户 @Bot 触发任务创建（从群消息上下文提取内容）
- 自动分享给群内已注册成员
- 支持选择 project 归属

## 12. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 架构模式 | P2P 任务分享 | 每个 CCM 平等，无中心节点 |
| 数据存储 | 代理模式，不同步 | 数据始终在分享者机器上，避免多端同步复杂度 |
| 飞书用途 | 仅身份绑定 + 通知 | 不建群不转发，大幅降低飞书集成复杂度 |
| 飞书应用 | 整个组织共用一个 | 一次创建，凭证分发 |
| 组织注册表 | 指定 CCM 兼任 | 零额外部署，可转移 |
| 分享粒度 | Task 级别 | 精确控制，按需分享 |
| 被分享者权限 | 查看 + 对话，config 只读 | task 所有权不变，被分享者不能修改 |
| 实时事件 | 前端直连分享者 WS | 不需要被分享者 CCM 代理 WS，更简单 |
| 多人消息 | 先发先执行，后发 pending | 和单人逻辑一致，task 不感知多人 |
| 消息标识 | 前缀 "[飞书用户 xxx]" | 分享者和 Claude 能区分消息来源 |
| 分享者离线 | 文字提示 | 不缓存，简单处理 |
| 前端入口 | Shares 按钮切换视图 | 不常态显示，点击才切换 |
