# GitHub PR Monitor — 实现方案

## 概述

为 CCM 新增 GitHub PR 自动审核功能。通过 GitHub Webhook 实时接收 PR 事件，自动创建 CCM task 让 Claude 审核代码，根据审核结果自动执行操作（merge / 写批注 / 修复代码）。

纯事件驱动，不轮询。

## 流程

```
GitHub PR created/updated
  → POST /api/github/webhook
  → HMAC-SHA256 验签
  → 创建 PRReview 记录
  → 自动创建 CCM task（Claude 审核）
  → Claude 读取 PR diff，审核代码
  → 根据结果执行操作：
      - 代码没问题 + auto_merge → approve + merge
      - 代码没问题 + 不 auto_merge → approve + LGTM 评论
      - 代码有问题 + auto_merge → 修复代码 + push + merge
      - 代码有问题 + 不 auto_merge → request-changes 批注
  → 更新 PRReview 状态
```

## 1. 数据库模型

### MonitoredRepo — 监控配置

```python
class MonitoredRepo(Base):
    __tablename__ = "monitored_repos"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    repo_full_name  = Column(String(200), unique=True, nullable=False)   # "owner/repo"
    project_id      = Column(Integer, nullable=True)                     # 关联 CCM project（可选）
    enabled         = Column(Boolean, default=True)                      # 开关
    auto_merge      = Column(Boolean, default=False)                     # 自动 merge 还是只审核
    webhook_secret  = Column(String(200), nullable=False)                # HMAC 验签密钥
    review_model    = Column(String(100), nullable=True)                 # 审核用的模型
    default_branch  = Column(String(100), default="main")               # 只监控目标为此分支的 PR
    status          = Column(String(20), default="active")               # active / error
    error_message   = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=func.now())
    updated_at      = Column(DateTime, nullable=True, onupdate=func.now())
```

### PRReview — 审核记录

```python
class PRReview(Base):
    __tablename__ = "pr_reviews"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    repo_id         = Column(Integer, ForeignKey("monitored_repos.id"), nullable=False, index=True)
    pr_number       = Column(Integer, nullable=False)
    pr_title        = Column(String(500), nullable=True)
    pr_author       = Column(String(200), nullable=True)
    pr_url          = Column(String(500), nullable=True)
    task_id         = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    status          = Column(String(30), default="pending")
    # pending → reviewing → approved / merged / commented / fix_pushed / error
    review_summary  = Column(Text, nullable=True)                       # Claude 的审核摘要
    action_taken    = Column(String(50), nullable=True)
    # approved_merged / lgtm_comment / fix_pushed_merged / review_comments / error
    created_at      = Column(DateTime, default=func.now())
    completed_at    = Column(DateTime, nullable=True)
```

### 索引

- `monitored_repos.repo_full_name` — unique
- `pr_reviews.repo_id` — 查询某仓库的审核历史
- `pr_reviews.task_id` — 关联 CCM task
- `pr_reviews(repo_id, pr_number)` — 去重检查

## 2. API 端点

### 仓库管理 `/api/pr-monitor/repos`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有监控仓库 |
| POST | `/` | 添加仓库（自动生成 webhook_secret） |
| GET | `/{id}` | 获取单个仓库配置 |
| PUT | `/{id}` | 更新配置 |
| DELETE | `/{id}` | 删除仓库 |
| POST | `/{id}/toggle` | 快速开关切换 |

### 审核记录

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{id}/reviews` | 该仓库的审核历史（分页） |
| GET | `/reviews/{review_id}` | 单条审核详情 |

### Webhook 接收

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/github/webhook` | GitHub 推送入口，公开无 Bearer auth |

## 3. Webhook 处理

### 验签流程

```python
@router.post("/api/github/webhook")
async def github_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    payload = json.loads(body)

    repo_name = payload.get("repository", {}).get("full_name", "")
    repo = await db.scalar(select(MonitoredRepo).where(MonitoredRepo.repo_full_name == repo_name))

    if not repo:
        return {"ok": False, "reason": "repo not monitored"}

    # HMAC-SHA256 验签
    expected = "sha256=" + hmac.new(
        repo.webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(403, "Invalid signature")
```

### 事件过滤

- `X-GitHub-Event` header 必须是 `pull_request`
- `action` 只处理 `opened`（新 PR）和 `synchronize`（PR 有新 commit）
- 忽略 `pull_request.draft == true`
- PR 目标分支必须匹配 `repo.default_branch`
- 仓库 `enabled` 必须为 True

### 去重

查询 `PRReview` 是否已有同一 `repo_id + pr_number` 且 `status` 不是 `error` 的记录：
- 如果有且状态是 `reviewing` → 对于 `synchronize` 事件，取消旧 task，创建新审核
- 如果有且状态是终态（merged/approved/commented）→ 对于 `synchronize` 事件，创建新审核
- 如果没有 → 创建新审核

### 安全

- Webhook 路径加入 `backend/middleware/auth.py` 的 `PUBLIC_PATHS`
- 用 HMAC-SHA256 替代 Bearer token 认证
- 用 `hmac.compare_digest` 防时序攻击

## 4. 审核 Task 创建

### 服务层

新文件 `backend/services/pr_review_service.py`：

```python
async def create_pr_review_task(
    db_factory,
    repo: MonitoredRepo,
    pr_data: dict,         # webhook payload 中的 pull_request 对象
) -> PRReview:
    """检测到新 PR 后创建审核 task。"""

    pr_review = PRReview(
        repo_id=repo.id,
        pr_number=pr_data["number"],
        pr_title=pr_data["title"],
        pr_author=pr_data["user"]["login"],
        pr_url=pr_data["html_url"],
        status="pending",
    )

    # 构建 prompt
    prompt = build_review_prompt(repo, pr_data)

    # 创建 CCM task
    task = Task(
        title=f"PR Review: {repo.repo_full_name}#{pr_data['number']} - {pr_data['title'][:80]}",
        description=prompt,
        project_id=repo.project_id,
        priority=5,
        mode="auto",
        model=repo.review_model,
        tags=["pr-review"],
        metadata_={"pr_review_id": pr_review.id, "repo_full_name": repo.repo_full_name},
    )

    pr_review.task_id = task.id
    pr_review.status = "reviewing"

    return pr_review
```

### Prompt 设计

```python
def build_review_prompt(repo: MonitoredRepo, pr_data: dict) -> str:
    pr_number = pr_data["number"]
    repo_name = repo.repo_full_name

    auto_merge_instructions = """
代码没有问题：
  gh pr review {pr_number} --repo {repo_name} --approve --body "LGTM - 代码审核通过"
  gh pr merge {pr_number} --repo {repo_name} --merge

代码有问题但可以修复：
  1. gh pr checkout {pr_number} --repo {repo_name}
  2. 修复问题
  3. git add + git commit + git push
  4. gh pr review {pr_number} --repo {repo_name} --approve --body "已修复问题并合并"
  5. gh pr merge {pr_number} --repo {repo_name} --merge

代码有严重问题无法自动修复：
  gh pr review {pr_number} --repo {repo_name} --request-changes --body "具体问题描述..."
""" if repo.auto_merge else """
代码没有问题：
  gh pr review {pr_number} --repo {repo_name} --approve --body "LGTM - 代码审核通过"

代码有问题：
  gh pr review {pr_number} --repo {repo_name} --request-changes --body "具体问题描述..."
"""

    return f"""你正在审核 {repo_name} 的 Pull Request #{pr_number}。

PR 标题: {pr_data["title"]}
PR 作者: {pr_data["user"]["login"]}
PR URL: {pr_data["html_url"]}
目标分支: {pr_data["base"]["ref"]}

## 步骤

### 1. 读取 PR 内容

gh pr view {pr_number} --repo {repo_name} --json title,body,files,additions,deletions,headRefName,baseRefName
gh pr diff {pr_number} --repo {repo_name}

### 2. 审核代码

检查以下方面：
- 正确性：逻辑错误、边界条件
- 安全性：注入、XSS、敏感信息泄露
- 性能：明显的性能问题
- 代码质量：可读性、命名、重复代码
- 测试：是否缺少必要的测试

### 3. 根据审核结果操作

{auto_merge_instructions}

### 4. 输出审核结果

操作完成后，在最后一行输出（必须严格遵循格式）：
PR_REVIEW_RESULT: <action>

action 取值：
- approved_merged — 审核通过并已合并
- lgtm_comment — 审核通过，已写 approve 评论
- fix_pushed_merged — 已修复问题并合并
- review_comments — 已写 request-changes 批注
- error — 操作失败
"""
```

## 5. Task 完成回调

在 `backend/services/dispatcher.py` 的 task 完成流程中添加钩子：

```python
async def _on_task_completed(self, task: Task):
    """Task 完成后检查是否是 PR 审核 task。"""
    metadata = task.metadata_ or {}
    pr_review_id = metadata.get("pr_review_id")
    if not pr_review_id:
        return

    async with self.db_factory() as db:
        review = await db.get(PRReview, pr_review_id)
        if not review:
            return

        # 从 task 输出中解析 PR_REVIEW_RESULT
        result = await self._parse_pr_review_result(task.id, db)

        review.status = {
            "approved_merged": "merged",
            "lgtm_comment": "approved",
            "fix_pushed_merged": "merged",
            "review_comments": "commented",
            "error": "error",
        }.get(result, "error")
        review.action_taken = result
        review.completed_at = datetime.utcnow()
        await db.commit()

async def _parse_pr_review_result(self, task_id: int, db: AsyncSession) -> str:
    """从 task 的最后几条日志中提取 PR_REVIEW_RESULT。"""
    logs = await db.execute(
        select(LogEntry.content)
        .where(LogEntry.task_id == task_id, LogEntry.event_type.in_(["message", "result"]))
        .order_by(LogEntry.id.desc())
        .limit(5)
    )
    for row in logs.scalars():
        if row and "PR_REVIEW_RESULT:" in row:
            return row.split("PR_REVIEW_RESULT:")[-1].strip()
    return "error"
```

## 6. 前端

### 页面：PRMonitorPage

路径：`frontend/src/pages/PRMonitorPage.tsx`

**列表视图**：
- 所有监控仓库，每行显示：仓库名、状态灯、开关、最近审核时间
- "添加仓库" 按钮

**仓库详情**（点击进入或右侧面板）：

配置区：
- auto_merge 开关
- review_model 选择器
- default_branch 输入
- Webhook 信息框：
  - URL：`https://youchengsong.claude-code-manager.com/api/github/webhook`（只读，带复制按钮）
  - Secret：遮掩显示，带复制按钮和重新生成按钮
  - 提示："请将以上 URL 和 Secret 配置到 GitHub 仓库的 Settings → Webhooks，事件选择 Pull requests"

审核历史表：
- 列：PR 号、标题、作者、状态 badge、操作结果、关联 task 链接、时间
- 状态颜色：pending=黄、reviewing=蓝、merged=绿、approved=绿、commented=橙、error=红

### 导航注册

- `Header.tsx`：添加 `{ key: 'pr-monitor', label: 'PR Monitor' }`
- `App.tsx`：添加页面路由

### API Client

```typescript
interface MonitoredRepo {
  id: number;
  repo_full_name: string;
  project_id: number | null;
  enabled: boolean;
  auto_merge: boolean;
  webhook_secret: string;
  review_model: string | null;
  default_branch: string;
  status: string;
  error_message: string | null;
  created_at: string;
}

interface PRReview {
  id: number;
  repo_id: number;
  pr_number: number;
  pr_title: string;
  pr_author: string;
  pr_url: string;
  task_id: number | null;
  status: string;
  review_summary: string | null;
  action_taken: string | null;
  created_at: string;
  completed_at: string | null;
}
```

## 7. 涉及的文件

### 新建

| 文件 | 说明 |
|------|------|
| `backend/models/pr_monitor.py` | MonitoredRepo + PRReview 模型 |
| `backend/schemas/pr_monitor.py` | Pydantic schemas |
| `backend/api/pr_monitor.py` | CRUD + Webhook API |
| `backend/services/pr_review_service.py` | 审核 task 创建逻辑 + prompt 构建 |
| `alembic/versions/xxx_add_pr_monitor.py` | 数据库 migration |
| `frontend/src/pages/PRMonitorPage.tsx` | 前端页面 |

### 修改

| 文件 | 改动 |
|------|------|
| `backend/main.py` | 注册 pr_monitor router |
| `backend/middleware/auth.py` | webhook 路径加入 PUBLIC_PATHS |
| `backend/services/dispatcher.py` | task 完成回调钩子 |
| `alembic/env.py` | 注册新 model |
| `frontend/src/api/client.ts` | 添加接口和 API 方法 |
| `frontend/src/App.tsx` | 注册页面路由 |
| `frontend/src/components/Layout/Header.tsx` | 添加导航项 |

## 8. 实现顺序

| 阶段 | 内容 | 预估 |
|------|------|------|
| Phase 1 | 数据库模型 + migration | 小 |
| Phase 2 | Schemas + CRUD API | 小 |
| Phase 3 | Webhook 处理器 + 验签 + auth 白名单 | 中 |
| Phase 4 | 审核 task 创建 + prompt 构建 | 中 |
| Phase 5 | Task 完成回调 | 小 |
| Phase 6 | 前端页面 | 大 |
| Phase 7 | 边界情况（PR 更新取消旧 task、WebSocket 实时推送） | 中 |
