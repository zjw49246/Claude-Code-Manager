# PR Monitor 实现 TODO

> **工作目录：`/home/ubuntu/Claude-Code-Manager`**
> 所有文件操作、git 命令、alembic 命令都在此目录下执行。
> **纯本地操作，不要 `git push`，不要上传到任何远程 repo。**

## 背景

为 CCM 新增 GitHub PR 自动审核功能。GitHub Webhook 推送 PR 事件 → 创建 CCM task 让 Claude 审核 → 审核通过自动 merge，有问题写 review comments。

### 关键决策

- GitHub token：使用机器上已有的 `gh auth`，不额外存储
- auto_merge 逻辑简化：审核通过 → approve + merge；有问题 → request-changes 评论，**不合并、不自动修复代码**
- 白名单作者：`allowed_authors` JSON 数组字段，空数组 = 不限制
- Task 完成判断：方案 B — 通过 `gh pr view` 检查 PR 实际状态，不依赖 Claude 输出格式解析
- 公网 URL：`https://youchengsong.claude-code-manager.com/api/github/webhook`
- 前端：独立完整页面 PRMonitorPage

---

## Phase 1: 数据库模型 + Migration

### Task 1.1: 创建 Model 文件 ✅

文件：`backend/models/pr_monitor.py`

```python
# MonitoredRepo 字段:
id, repo_full_name (unique), project_id (nullable), enabled (default True),
auto_merge (default False), webhook_secret, review_model (nullable),
default_branch (default "main"), allowed_authors (JSON, default []),
status (default "active"), error_message (nullable),
created_at, updated_at

# PRReview 字段:
id, repo_id (FK monitored_repos.id, indexed), pr_number,
pr_title, pr_author, pr_url, task_id (FK tasks.id, nullable, indexed),
status (default "pending"), review_summary (nullable),
action_taken (nullable), created_at, completed_at (nullable)
# status 流转: pending → reviewing → approved / merged / commented / error / superseded
# action_taken: approved_merged / lgtm_comment / review_comments / error
```

### Task 1.2: 注册 Model ✅

文件：`backend/models/__init__.py` — import MonitoredRepo 和 PRReview

文件：`alembic/env.py` — 确保新 model 被 import（检查现有 pattern）

### Task 1.3: 生成 Migration ✅

```bash
cd /home/ubuntu/Claude-Code-Manager
source .venv/bin/activate
alembic revision --autogenerate -m "add_pr_monitor_tables"
```

验证生成的 migration 文件，确认包含 `monitored_repos` 和 `pr_reviews` 两张表。

### Task 1.4: 执行 Migration ✅

```bash
alembic upgrade head
```

验证表已创建：`sqlite3 claude_manager.db ".tables"` 应包含新表。

---

## Phase 2: Pydantic Schemas + CRUD API

### Task 2.1: 创建 Schema 文件 ✅

文件：`backend/schemas/pr_monitor.py`

```python
# MonitoredRepoCreate: repo_full_name, project_id?, auto_merge?, review_model?,
#                      default_branch?, allowed_authors?
# MonitoredRepoUpdate: 所有字段可选
# MonitoredRepoResponse: 所有字段 + id + created_at + updated_at
#   注意 webhook_secret 在 response 中遮掩显示（只显示前4位 + ***）
# PRReviewResponse: 所有字段
```

### Task 2.2: 创建 API 路由 ✅

文件：`backend/api/pr_monitor.py`

路由前缀：`/api/pr-monitor`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/repos` | 列出所有监控仓库 |
| POST | `/repos` | 添加仓库（自动生成 webhook_secret） |
| GET | `/repos/{id}` | 获取单个仓库配置（返回完整 secret） |
| PUT | `/repos/{id}` | 更新配置 |
| DELETE | `/repos/{id}` | 删除仓库及其审核记录 |
| POST | `/repos/{id}/toggle` | 快速开关 enabled |
| POST | `/repos/{id}/regenerate-secret` | 重新生成 webhook_secret |
| GET | `/repos/{id}/reviews` | 该仓库的审核历史（分页，默认 page=1, size=20） |
| GET | `/reviews/{review_id}` | 单条审核详情 |

webhook_secret 生成用 `secrets.token_hex(32)`。

### Task 2.3: 注册路由到 main.py ✅

文件：`backend/main.py`

- import `from backend.api.pr_monitor import router as pr_monitor_router`
- `app.include_router(pr_monitor_router)`

### Task 2.4: 测试 CRUD API ✅

用 curl 测试：
```bash
# 创建仓库
curl -X POST http://localhost:8321/api/pr-monitor/repos \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"repo_full_name": "test/repo", "auto_merge": true, "allowed_authors": ["user1"]}'

# 列出仓库
curl http://localhost:8321/api/pr-monitor/repos -H "Authorization: Bearer <token>"
```

---

## Phase 3: Webhook 处理器

### Task 3.1: 添加 Webhook 路径到白名单 ✅

文件：`backend/middleware/auth.py`

在 `PUBLIC_PATHS` 中添加 `"/api/github/webhook"`。

### Task 3.2: 实现 Webhook 端点 ✅

文件：`backend/api/pr_monitor.py`（在同一文件中添加）

路由：`POST /api/github/webhook`

处理流程：
1. 读取 raw body
2. 从 payload 提取 `repository.full_name`
3. 查询 MonitoredRepo（如果不存在或 disabled → 返回 200 + ignore）
4. HMAC-SHA256 验签（用 `hmac.compare_digest`）
5. 检查 `X-GitHub-Event` header == `pull_request`
6. 检查 `action` in (`opened`, `synchronize`)
7. 跳过 draft PR
8. 检查 PR 目标分支 == `repo.default_branch`
9. 检查 PR 作者是否在 `allowed_authors` 中（空数组 = 不限制）
10. 去重检查（同 repo_id + pr_number 是否有进行中的审核）
11. 对于 `synchronize`：标记旧审核为 `superseded`
12. 调用 `pr_review_service.create_pr_review_task()`

返回 200（GitHub 期望快速响应，实际处理异步进行）。

### Task 3.3: 测试 Webhook ✅

用 curl 模拟 GitHub webhook 调用（构造正确的 HMAC 签名）验证验签和事件过滤。

---

## Phase 4: 审核 Task 创建 + Prompt

### Task 4.1: 创建 PR Review Service ✅

文件：`backend/services/pr_review_service.py`

```python
async def create_pr_review_task(db, repo: MonitoredRepo, pr_data: dict) -> PRReview:
    """
    1. 创建 PRReview 记录 (status=pending)
    2. 构建审核 prompt (build_review_prompt)
    3. 创建 Task 记录 (mode="auto", tags=["pr-review"], metadata_={"pr_review_id": ...})
    4. 更新 PRReview: task_id, status="reviewing"
    5. commit
    6. 通过 WebSocket 广播通知前端
    """

def build_review_prompt(repo: MonitoredRepo, pr_data: dict) -> str:
    """
    构建审核 prompt，关键点:
    - 第一步: gh pr view + gh pr diff 读取 PR 内容
    - 第二步: 审核代码（正确性、安全性、性能、代码质量、测试）
    - 第三步: 根据 auto_merge 决定操作:
      * auto_merge=True + 通过 → gh pr review --approve + gh pr merge --merge
      * auto_merge=True + 有问题 → gh pr review --request-changes (不合并)
      * auto_merge=False + 通过 → gh pr review --approve + LGTM 评论
      * auto_merge=False + 有问题 → gh pr review --request-changes
    - 最后一行输出: PR_REVIEW_RESULT: <action>
    """
```

### Task 4.2: 在 Webhook 处理中调用 Service ✅

确保 webhook handler 创建完 PRReview 后，task 能被 dispatcher 正常拾取和执行。

需要注意：
- Task 创建后 dispatcher 会自动调度（检查现有 dispatcher 的 task 拾取逻辑）
- 确保 pr-review task 的 `target_repo` 设置合理（可以为空，因为 Claude 用 gh CLI 远程操作）

---

## Phase 5: Task 完成回调

### Task 5.1: 在 dispatcher 中添加回调钩子 ✅

文件：`backend/services/dispatcher.py`

在 task 完成处（约 642-662 行，`=== Claude Code completed successfully ===` 之后）添加：

```python
# 检查是否是 PR review task
await self._handle_pr_review_completion(task)
```

实现 `_handle_pr_review_completion`:
1. 检查 `task.metadata_` 中是否有 `pr_review_id`
2. 如果有，调用 `pr_review_service.check_and_update_review(pr_review_id, repo)`
3. `check_and_update_review` 通过 `gh pr view {pr_number} --repo {repo_name} --json state,mergedAt,reviews` 检查 PR 实际状态
4. 根据实际状态更新 PRReview: status, action_taken, completed_at, review_summary
5. 通过 WebSocket 广播更新

### Task 5.2: 处理 Task 失败的情况 ✅

在 dispatcher 的 task 失败处也添加回调，将 PRReview 标记为 error。

---

## Phase 6: 前端页面

### Task 6.1: 添加 API Client 方法 ✅

文件：`frontend/src/api/client.ts`

添加接口定义和 API 方法：
- `MonitoredRepo` interface
- `PRReview` interface
- `getMonitoredRepos()`, `createMonitoredRepo()`, `updateMonitoredRepo()`,
  `deleteMonitoredRepo()`, `toggleMonitoredRepo()`, `regenerateSecret()`,
  `getRepoReviews()`, `getReviewDetail()`

### Task 6.2: 创建 PRMonitorPage 组件 ✅

文件：`frontend/src/pages/PRMonitorPage.tsx`

**列表视图**（默认）：
- 表格显示所有监控仓库：仓库名、状态指示灯、enabled 开关、auto_merge 标签、最近审核时间
- 右上角「添加仓库」按钮 → 弹出表单 modal
- 点击仓库行 → 进入详情视图

**仓库详情视图**：
- 返回按钮
- 配置区（可编辑）:
  - repo_full_name（只读）
  - auto_merge 开关
  - review_model 输入
  - default_branch 输入
  - allowed_authors 编辑（tag input 形式）
  - Webhook 配置信息框:
    - URL: `https://youchengsong.claude-code-manager.com/api/github/webhook`（只读 + 复制按钮）
    - Secret: 遮掩显示 + 复制按钮 + 重新生成按钮
    - 配置提示文字
- 审核历史表:
  - 列: PR 号、标题、作者、状态 badge、操作结果、关联 task 链接、时间
  - 状态颜色: pending=黄、reviewing=蓝、merged=绿、approved=绿、commented=橙、error=红、superseded=灰
  - 分页

### Task 6.3: 注册页面路由和导航 ✅

文件：`frontend/src/App.tsx`
- import PRMonitorPage
- 添加 `{page === 'pr-monitor' && <PRMonitorPage />}`

文件：`frontend/src/components/Layout/Header.tsx`
- 在导航项数组中添加 `{ key: 'pr-monitor', label: 'PR Monitor' }`

### Task 6.4: 构建前端 ✅

```bash
cd /home/ubuntu/Claude-Code-Manager/frontend && npm run build
```

验证无报错。

---

## Phase 7: 集成测试 + 收尾

### Task 7.1: 端到端测试 ✅

1. 启动后端服务
2. 通过前端添加一个测试仓库
3. 用 curl 模拟 GitHub webhook（构造 HMAC 签名）
4. 验证：PRReview 记录创建 → Task 被创建 → Task 列表可见
5. 验证前端页面显示正常

### Task 7.2: WebSocket 实时更新 ✅

确保 PRReview 状态变更时通过 WebSocket 广播，前端能实时刷新。
使用现有的 `ws_broadcaster` 机制，事件 channel 用 `pr-monitor`。

### Task 7.3: 更新文档 ✅

- 更新 `CLAUDE.md`：添加 PR Monitor 相关的架构说明
- 更新 `README.md`：添加 PR Monitor 功能说明和配置指南
- 更新 `TEST.md`：添加 PR Monitor 测试用例

### Task 7.4: 提交代码 ✅

```bash
cd /home/ubuntu/Claude-Code-Manager
git add -A && git commit -m "feat: add PR Monitor - GitHub PR auto-review via webhook"
```

**仅本地提交，禁止 `git push`，禁止上传到任何远程仓库。**
