# Team CCM 方案设计

> 本文档是 CCM 多用户团队模式的完整设计方案。

## 1. 核心思路

**本质：所有数据汇聚到中心 CCM，按用户权限过滤显示。**

现有多台独立运行的 CCM 实例，通过"收养"变为中心 CCM 的 Worker。所有 Worker 上的 Task/Project 通过 WorkerRelay 实时同步到中心 CCM 数据库。用户在中心 CCM 注册账号、登录后，只看到自己有权限的内容。

```
中心 CCM（管理面板，所有数据汇聚于此）
├── User: Admin（你，全局可见）
├── User: Alice → 专属 Worker-A, Worker-B
├── User: Bob → 专属 Worker-C
├── User: Charlie → 无专属 Worker，被分享了部分 Project/Task
│
├── Worker-A（Alice 的前 CCM，被收养）
├── Worker-B（新创建，分配给 Alice）
├── Worker-C（Bob 的前 CCM，被收养）
├── Worker-D, E（公共池，Admin 管理）
│
└── WorkerRelay 把所有 Worker 的数据同步到中心 DB
    └── 按 User 权限过滤显示
```

---

## 2. 用户系统

### 2.1 User 模型

```sql
User
  id              SERIAL PRIMARY KEY
  email           VARCHAR UNIQUE NOT NULL
  name            VARCHAR NOT NULL
  avatar_url      VARCHAR DEFAULT ''
  role            VARCHAR DEFAULT 'member'  -- 'admin' | 'member'
  password_hash   VARCHAR NOT NULL
  feishu_open_id  VARCHAR DEFAULT ''        -- 可选飞书绑定
  feishu_name     VARCHAR DEFAULT ''
  created_at      TIMESTAMP
  last_login_at   TIMESTAMP
```

### 2.2 角色

- **Admin**: 全局可见所有内容，管理用户/Worker/权限，可以有多个 Admin
- **Member**: 只看到自己有权限的内容

### 2.3 认证流程

**注册**：
1. Email + 密码注册（需要 Admin 预设的邀请码或 Admin 手动开通）
2. 注册后可选绑定飞书（用于接收通知）

**登录**：
- JWT token 认证（替代现有单 token）
- Token 包含 user_id 和 role
- 向后兼容：环境变量 `AUTH_TOKEN` 存在时，该 token 视为 Admin 登录（单用户模式）

**飞书绑定**：
- 从"全局唯一绑定"改为"per-user 绑定"
- 每个 User 独立 OAuth 绑定自己的飞书账号
- 用于：接收任务通知、消息提醒

---

## 3. 权限体系

### 3.1 两级权限

```
Worker 权限（最高）
  └── 看到 Worker 上所有 Project 和 Task
  └── 可创建/删除 Project、Task，完全控制
  └── Worker 独占分配，一个 Worker 只给一个用户

Project 权限
  └── 看到该 Project 下被分享的 Task
  └── 可在该 Project 下创建 Task、发消息
  └── 前提：必须先有 Project 权限，才能被分享 Task
```

**没有单独的 Task 权限** —— Task 的可见性依赖 Project 权限。Admin 分享 Task 给用户时，该用户必须已有对应 Project 的权限。

### 3.2 权限分享表

```sql
ProjectShare
  id              SERIAL PRIMARY KEY
  project_id      INT NOT NULL          -- 分享哪个 Project
  target_type     VARCHAR NOT NULL      -- 'user' | 'group'
  target_id       INT NOT NULL          -- user.id 或 group.id
  shared_by       INT NOT NULL          -- 操作者 user.id (Admin)
  created_at      TIMESTAMP

TaskShare
  id              SERIAL PRIMARY KEY
  task_id         INT NOT NULL          -- 分享哪个 Task
  target_type     VARCHAR NOT NULL      -- 'user' | 'group'
  target_id       INT NOT NULL          -- user.id 或 group.id
  shared_by       INT NOT NULL          -- 操作者 user.id (Admin 或 Task 创建者)
  created_at      TIMESTAMP
```

### 3.3 小组（Group）

保留现有的小组概念，改为基于 User 而非 feishu_open_id：

```sql
UserGroup
  id              SERIAL PRIMARY KEY
  name            VARCHAR NOT NULL
  description     VARCHAR DEFAULT ''
  created_at      TIMESTAMP

UserGroupMember
  group_id        INT NOT NULL
  user_id         INT NOT NULL
  PRIMARY KEY (group_id, user_id)
```

分享 Project/Task 时，`target_type = 'group'` 表示分享给整个小组。

### 3.4 可见性规则

```python
def get_visible_tasks(user):
    if user.role == 'admin':
        return all_tasks

    visible = set()

    # 1. 专属 Worker 上的所有 Task
    for w in workers_where(owner_user_id=user.id):
        visible |= tasks_where(worker_id=w.id)

    # 2. 用户自己创建的 Task
    visible |= tasks_where(created_by=user.id)

    # 3. 被分享了 Project 权限，且 Task 也被分享
    shared_project_ids = get_shared_project_ids(user)
    for pid in shared_project_ids:
        shared_task_ids = get_shared_task_ids(user, project_id=pid)
        visible |= tasks_where(id__in=shared_task_ids)

    return visible

def get_visible_projects(user):
    if user.role == 'admin':
        return all_projects

    visible = set()

    # 1. 专属 Worker 上的所有 Project
    for w in workers_where(owner_user_id=user.id):
        visible |= projects_on_worker(w.id)

    # 2. 被分享的 Project
    visible |= get_shared_project_ids(user)

    # 3. 自己创建的 Task 所属的 Project
    visible |= projects_of_tasks_created_by(user.id)

    return visible
```

### 3.5 分享流程

**Admin 分享 Task 给用户：**
```
1. 选择 Task #100（属于 Project-X）
2. 选择目标：用户 Alice 或 小组"前端组"
3. 系统检查：Alice 有 Project-X 的权限吗？
   ├── 有 → 直接创建 TaskShare
   └── 没有 → 提示"Alice 没有 Project-X 权限，是否一并分享？"
       └── 确认 → 先创建 ProjectShare，再创建 TaskShare
```

### 3.6 完整权限矩阵

| 操作 | Admin | Member（有 Worker） | Member（有 Project 权限） |
|------|-------|-------------------|----------------------|
| 创建/销毁 Worker | 可以 | 不可以 | 不可以 |
| 分配 Worker 给用户 | 可以 | 不可以 | 不可以 |
| 收养已有 CCM | 可以 | 不可以 | 不可以 |
| 在公共池创建 Project | 可以 | 不可以 | 不可以 |
| 分享 Project/Task | 可以 | 不可以 | 不可以 |
| 管理小组 | 可以 | 不可以 | 不可以 |
| 管理用户 | 可以 | 不可以 | 不可以 |
| 在自己 Worker 上创建 Project | - | 可以 | - |
| 在自己 Worker 上创建/删除 Task | - | 可以 | - |
| 在被分享的 Project 下创建 Task | - | - | 可以 |
| 发消息 | 可以 | 可以 | 可以 |
| 控制自己的 Worker（开/关/重启） | 可以 | 可以 | 不可以 |
| 看其他人的内容 | 全部可见 | 只看自己 Worker | 只看被分享的 |

---

## 4. Worker 管理

### 4.1 Worker 分配

Worker 表新增字段：

```sql
ALTER TABLE workers ADD COLUMN owner_user_id INT REFERENCES users(id);
-- NULL = 公共池（Admin 管理）
-- 非 NULL = 专属于该用户
```

**规则：**
- Worker 独占分配，一个 Worker 只能分配给一个用户
- 一个用户可以拥有多个 Worker
- 每个 Worker 最多 5 个 Claude Code 账号（防封号）
- 公共池 Worker（`owner_user_id = NULL`）由 Admin 管理
- 分配后，用户可自行控制该 Worker（开/关/重启），但不能创建/销毁 Worker

### 4.2 创建新 Worker

复用现有 Worker 创建流程（见 elastic-worker-design.md §3）。新增：
- 创建时可选指定 `owner_user_id`
- 不指定则进入公共池

### 4.3 收养已有 CCM 为 Worker

**场景：** 现有 6-7 台独立运行的 CCM 实例，需要纳入中心管理。

**收养流程：**

```
Admin 操作：收养已有 CCM
┌──────────────────────────────────────────────────┐
│  目标 CCM:                                        │
│    IP/域名: [ 10.0.1.50                     ]    │
│    SSH Key: [ 选择已有 key           ▼ ]          │
│    分配给:  [ Alice                  ▼ ]          │
│                                                   │
│              [ 取消 ]  [ 收养 ]                    │
└──────────────────────────────────────────────────┘
```

**收养 Pipeline：**

```
1. SSH 连接验证
   └── 确认目标机器可达、SSH key 有效

2. 版本对齐
   ├── rsync 中心 CCM 代码到目标机器
   ├── 保留目标机器的 .env（Claude 账号池等）
   └── 保留目标机器的数据库（历史 Task 数据）

3. 配置注入
   ├── 注入中心 CCM 的 AUTH_TOKEN（Worker 认证用）
   └── 设置 WORKER_MODE=true（标记为 Worker 模式）

4. 服务重启
   └── 重启目标机器的 CCM 服务（使用新代码）

5. 注册为 Worker
   ├── 中心 CCM 创建 Worker 记录
   ├── 设置 owner_user_id（分配给谁）
   ├── 状态设为 ready
   └── 启动 WorkerRelay 连接

6. 数据回填
   ├── WorkerRelay 连接后，自动 backfill 历史 Task/Chat 数据
   └── 目标机器上的现有 Task 同步到中心 DB
```

**与新建 Worker 的区别：**

| 步骤 | 新建 Worker | 收养已有 CCM |
|------|------------|-------------|
| EC2 实例 | 新创建 | 已存在 |
| 代码部署 | rsync 全新 | rsync 覆盖，保留 .env 和 DB |
| Claude 账号 | 需要填写并登录 | 已有账号，保留 |
| 历史数据 | 无 | 有，需要 backfill 同步 |
| SSH Key | 自动从 Manager 继承 | 需要手动指定 |

**收养后的变化：**
- 被收养的 CCM 成为 Worker 模式，不再独立对外服务
- 原来的用户改为通过中心 CCM 登录访问
- 该 Worker 上的所有 Task/Project 通过 relay 镜像到中心
- 分配给用户后，只有该用户和 Admin 可见

### 4.4 Worker 视角差异

**Admin 看到：**
```
Workers 页面
├── Worker-A (Alice) — online, 3 tasks, 5 CC 账号
├── Worker-B (Alice) — online, 1 task, 3 CC 账号
├── Worker-C (Bob) — online, 2 tasks, 4 CC 账号
├── Worker-D (公共池) — online, 0 tasks, 5 CC 账号
└── Worker-E (公共池) — stopped
```

**Member (Alice) 看到：**
```
My Workers
├── Worker-A — online, 3 tasks
└── Worker-B — online, 1 task
(看不到 Bob 的 Worker 和公共池)
```

---

## 5. Task/Project 模型变更

### 5.1 Task 新增字段

```sql
ALTER TABLE tasks ADD COLUMN created_by INT REFERENCES users(id);
```

- `created_by`：谁创建的（User.id）
- 现有 `worker_id`：在哪个 Worker 上运行
- 现有 `project_id`：属于哪个 Project

### 5.2 消息用户标识

共享 Task 中多人发消息时，加用户前缀区分：

```
[游承松] 这个功能看一下
[Alice] 好的在处理
[Bob] 我这边也有相关的改动
```

**实现方式：**
- 发消息 API 根据 JWT token 识别当前用户
- 消息内容前加 `[user.name]` 前缀
- 前端渲染时解析前缀，显示对应头像和名字
- 用户在自己 Worker 上的 Task 里发消息不加前缀（只有自己）

---

## 6. 前端视图

### 6.1 Admin 视图

Admin 登录后看到完整管理界面：

```
Dashboard
├── 用户概览：各用户任务统计
├── Worker 概览：全部 Worker 状态
└── 全局 Task 统计

Tasks 页面
├── 筛选：按用户 / 按 Project / 按 Worker
├── 看到所有 Task
└── 可分享/分配给任何用户

Workers 页面（复用现有）
├── 所有 Worker
├── 创建/销毁/收养
└── 分配给用户

Users 页面（新增）
├── 用户列表（name, email, role, 飞书绑定状态）
├── 创建/删除用户
├── 分配 Worker
└── 管理小组

Sharing 页面（新增或集成到 Task/Project 页面）
├── Project 分享管理
└── Task 分享管理
```

### 6.2 Member 视图

Member 登录后看到精简界面：

```
My Tasks
├── 自己 Worker 上的 Task
├── 被分享的 Task
└── 自己创建的 Task

My Projects
├── 自己 Worker 上的 Project
└── 被分享的 Project

My Workers（如果有专属 Worker）
├── Worker 状态
├── 开/关/重启
└── Instance 管理
```

**不显示：** Users、Sharing、公共池 Worker、其他人的内容

---

## 7. 迁移策略

### 7.1 从现有架构迁移

```
Phase 0: 准备
  ├── 选定一台作为中心 CCM
  └── 确认所有机器 SSH 可达

Phase 1: 中心 CCM 升级
  ├── 部署 User 系统 + JWT 登录
  ├── Admin 账号自动创建（当前 AUTH_TOKEN 持有者）
  └── 单用户模式向后兼容（无 User 表时用 token）

Phase 2: 收养 Worker
  ├── 逐台收养其他 CCM 为 Worker
  ├── 每台收养后验证 relay 同步
  └── 分配给对应用户

Phase 3: 用户注册
  ├── 为每个现有 CCM 使用者创建 User 账号
  ├── 绑定飞书（可选）
  └── 通知他们改用中心 CCM 登录

Phase 4: 关闭独立访问
  └── 被收养的 CCM 不再对外暴露（只接受中心 CCM 的内网连接）
```

### 7.2 新用户加入流程

```
1. Admin 创建 User 账号（或用户自注册 + Admin 审批）
2. 用户登录中心 CCM
3. 如需独立环境：
   ├── Admin 创建新 Worker
   └── 分配给该用户
4. 如不需独立环境：
   ├── Admin 分享 Project/Task 给该用户
   └── Task 在公共池 Worker 上运行
```

---

## 8. 实施阶段

### Phase 1: User 模型 + JWT 登录
- User 表 + Alembic migration
- JWT 认证中间件（替代单 token，向后兼容）
- 注册/登录 API
- 前端登录页改造（email + password）
- Admin 自动创建

### Phase 2: 权限过滤
- Worker 表加 `owner_user_id`
- Task 表加 `created_by`
- ProjectShare / TaskShare 表
- API 层加可见性过滤
- 分享 API（Admin 操作）

### Phase 3: 前端分视图
- Admin 管理界面（用户管理、全局 Task、Worker 分配）
- Member 精简界面（只看自己的）
- 分享 UI（选用户/小组）

### Phase 4: 收养已有 CCM
- 收养 Pipeline（SSH + rsync + 配置注入 + 注册）
- 收养 UI（填 IP + SSH key + 分配用户）
- 历史数据 backfill

### Phase 5: 飞书 per-user 绑定
- FeishuBinding 改为关联 User.id
- 每个用户独立 OAuth
- 通知按用户发送

### Phase 6: 小组 + 消息
- UserGroup / UserGroupMember 表
- 分享给小组
- 消息加用户前缀

---

## 9. 与现有功能的兼容

| 现有功能 | 兼容方式 |
|---------|---------|
| 单 token 认证 | `AUTH_TOKEN` 存在时作为 Admin token，无 User 表时退回单用户模式 |
| Worker 系统 | 新增 `owner_user_id` 字段，现有 Worker 功能不变 |
| WorkerRelay | 不变，继续同步所有数据到中心 |
| TaskMigrator | 不变，Task 迁移时携带 `created_by` |
| Claude Pool | 每个 Worker 独立管理自己的账号池，中心不干预 |
| 飞书通知 | 改为 per-user，按 Task 可见性决定通知谁 |
| 现有分享系统 | 逐步替换为 ProjectShare + TaskShare，旧的 org registry 联邦制可共存 |

---

## 10. 安全隔离：共享 Project 容器化

### 10.1 问题

Claude Code 本质上是有 shell 权限的 agent。用户通过 Task 发消息时，Claude Code 可以操作 Worker 上的文件系统、读取环境变量、SSH 到其他机器。如果多个用户共享一个 Worker 上的 Project，存在穿透风险：

- 读取宿主机 `.env`（含其他用户的 API key）
- 访问同 Worker 上其他 Project 的代码
- SSH 到其他 Worker
- 使用宿主机的 GitHub token 访问任意 repo

### 10.2 隔离策略

**核心规则：专属 Worker 裸跑，共享 Project 容器跑。**

| 场景 | 隔离方式 | 原因 |
|------|---------|------|
| 专属 Worker（用户自己的） | 裸跑，无隔离 | 整台机器是他的，没有风险 |
| 共享 Project（只读查看） | 不需要隔离 | 只看对话记录，不执行操作 |
| 共享 Project（可操作） | Docker 容器 | 多人操作同一环境，需要隔离 |

### 10.3 容器架构

```
公共 Worker 宿主机
├── Docker volumes
│   ├── project-101/              ← 持久化 volume
│   │   ├── repo/                 ← git clone 的代码
│   │   ├── worktrees/            ← 各 Task 的 git worktree
│   │   ├── .ssh/deploy_key       ← 该 repo 专用的 SSH key
│   │   └── .claude/              ← Claude Code 配置
│   │
│   └── project-102/              ← 另一个 Project，完全隔离
│
├── Container (project-101)
│   ├── 挂载: project-101 volume → /workspace
│   ├── Claude Code 只能访问 /workspace
│   ├── 没有宿主机的 SSH key、GitHub token、.env
│   ├── 独立的 Claude 账号（从宿主机 pool 分配一个）
│   └── 网络: 只能访问 Claude API，不能 SSH 其他机器
│
└── 宿主机的其他文件/凭证
    └── 容器完全看不到
```

### 10.4 GitHub 权限：Deploy Key 做 repo 级隔离

容器化带来一个额外好处——GitHub 权限精确到 repo 级别：

```
现在（裸跑）：
  宿主机一个 GitHub token → 能访问所有 repo → 任何 Task 都能操作任意仓库

容器方案：
  每个 Project 容器有自己 repo 的 Deploy Key → 只能操作这一个 repo
```

**Admin 创建共享 Project 时：**
1. 填写 GitHub repo URL
2. 系统生成一对 SSH key（Project 专用）
3. Admin 把公钥添加到 GitHub repo 的 Deploy Keys（read-write）
4. 私钥存在容器 volume 里，配置 git 使用它
5. clone repo 到容器的 `/workspace/repo`

容器内的 git 操作完全正常：
```bash
git pull origin main        # 用 deploy key 认证
git checkout -b feature-x
git add . && git commit
git push origin feature-x   # 正常 push
```

### 10.5 多用户共享同一容器

容器隔离的是**宿主机环境**，不是用户之间。同一个 Project 的所有授权用户共享同一个容器/volume：

```
Container (Project-101: github.com/company/frontend)
├── /workspace/repo/                     ← 共享代码
│
├── Alice 创建 Task #1 → docker exec → 在容器内跑 Claude Code
│   └── git worktree: /workspace/worktrees/task-fix-login/
│
├── Bob 创建 Task #2 → docker exec → 在容器内跑 Claude Code
│   └── git worktree: /workspace/worktrees/task-add-search/
│
└── 所有人看到同一份代码，不同 Task 用 worktree 隔离分支
```

**并发控制**（现有机制已覆盖）：
- 不同 Task 在不同 git worktree，互不干扰
- Dispatcher 已有 per-project 串行控制
- git push 冲突由 Claude Code 工作流的 rebase 逻辑处理

### 10.6 容器生命周期

```
创建：Admin 创建共享 Project 时
  → docker volume create ccm-project-{id}
  → docker run 启动容器（基础镜像含 Claude Code + git + 常用工具）
  → git clone repo 到 /workspace/repo

运行中：
  → 容器常驻，Task 通过 docker exec 进入执行
  → volume 持久化，容器重启不丢数据
  → Claude 账号从宿主机 pool 分配（通过环境变量传入）

销毁：Admin 删除 Project 时
  → 停止容器内所有 Task
  → docker rm 容器
  → volume 可选保留备份或直接删除
```

### 10.7 实现改动

核心改动在 `instance_manager.launch()`：

```python
# instance_manager.launch() 
if project.is_shared_container:
    # 共享 Project → 在容器里跑
    container = get_or_create_container(project.id)
    process = docker_exec(container, "claude", "-p", prompt, ...)
else:
    # 专属 Worker 或本机 → 裸跑（现有逻辑）
    process = subprocess.Popen(["claude", "-p", prompt, ...])
```

Project 模型新增：
```sql
ALTER TABLE projects ADD COLUMN is_shared_container BOOLEAN DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN container_id VARCHAR DEFAULT '';
ALTER TABLE projects ADD COLUMN deploy_key_path VARCHAR DEFAULT '';
```

### 10.8 权限模型总结

```
专属 Worker 用户
  → 裸跑，完整权限，整台机器是他的
  → GitHub: 用 Worker 上已有的 token/key

共享 Project（可操作）
  → 容器隔离，只能操作自己 Project 的代码
  → GitHub: 只有该 repo 的 Deploy Key
  → 碰不到宿主机、其他 Project、其他 Worker

共享 Task（只读）
  → 只能查看对话记录和结果
  → 不执行任何操作
  → Admin 可选开启发消息权限
```

---

### 10.9 容器安全加固

容器隔离解决了文件系统和网络穿透，但还需要防御容器内的提权和资源滥用。

**容器启动参数：**

```yaml
docker run \
  --security-opt no-new-privileges \   # 禁止提权
  --cap-drop ALL \                      # 丢弃所有 Linux 能力
  --read-only \                         # 根文件系统只读
  --tmpfs /tmp:size=1g \                # /tmp 可写但不持久，限 1G
  -v project-volume:/workspace \        # 只有工作目录可写
  --network ccm-restricted \            # 受限网络
  --memory 4g \                         # 内存上限
  --cpus 2 \                            # CPU 上限
  --pids-limit 100 \                    # 进程数上限（防 fork bomb）
  --user 1000:1000 \                    # 非 root 用户运行
  ccm-sandbox:latest
```

**受限网络配置：**

```bash
# 创建受限网络：只允许出站到 Claude API，禁止内网访问
docker network create ccm-restricted \
  --driver bridge \
  --opt com.docker.network.bridge.enable_icc=false

# iptables 规则（在宿主机上）
# 允许：Claude API (api.anthropic.com)、GitHub (github.com)、DNS
# 禁止：内网 10.0.0.0/8、其他 Worker、宿主机端口
```

**基础镜像要求（ccm-sandbox）：**

| 包含 | 不包含 |
|------|--------|
| Claude Code CLI | Docker CLI（防止套娃） |
| git, ssh-client | sudo / su |
| Node.js, Python | 包管理器的 root 权限 |
| 常用开发工具 | 网络扫描工具（nmap 等） |

**防御矩阵：**

| 攻击方式 | 防御措施 |
|---------|---------|
| 读宿主机文件 | 容器文件系统隔离，只挂载 /workspace |
| 提权到 root | `no-new-privileges` + `cap-drop ALL` + 非 root 用户 |
| 容器逃逸 | `read-only` 根文件系统 + 无特权能力 |
| Docker 套娃 | 不安装 Docker CLI，不挂载 docker.sock |
| 挖矿 / 资源滥用 | CPU/内存/进程数限制 |
| SSH 穿透其他机器 | 受限网络，禁止内网访问 |
| 安装恶意包 | 根文件系统只读，只有 /workspace 和 /tmp 可写 |
| fork bomb | `--pids-limit 100` |
| 长时间占用 | 现有 4 小时 Task 超时兜底 |

---

## 11. 约束与限制

1. **每 Worker 最多 5 个 CC 账号** — 防止单机账号过多被封
2. **Worker 创建权只在 Admin** — 成本控制
3. **Worker 独占** — 分配后只有一个用户可用
4. **数据最终一致** — relay 同步有延迟（通常毫秒级），极端情况下秒级
5. **收养不可逆** — 收养后的 CCM 成为 Worker，不建议再独立运行
6. **共享 Project 需要 Docker** — 公共池 Worker 必须安装 Docker
7. **Deploy Key 需手动添加** — Admin 创建共享 Project 后需要去 GitHub 添加 Deploy Key
