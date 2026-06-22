# Project Todo List 设计方案（定稿）

> 本文档为 PR #30 的定稿设计，已纳入 code review（youchengsong）意见与实现期的取舍。

## 概述

每个 Project 维护一个 Todo List，用来管理待执行的任务 prompt。Todo 可以一键创建 Task 并跳转到 Chat，创建成功后该 Todo 自动标记为 done 并记录它派生的 task。参数全部使用系统默认值，创建后可在 Task 详情里修改。

## 数据模型

### 新增表：`project_todos`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 关联 projects(id)，ON DELETE CASCADE（见下方级联说明） |
| title | VARCHAR(200) NOT NULL | 标题 |
| prompt | TEXT NOT NULL | 任务 prompt |
| status | VARCHAR(20) DEFAULT 'open' | open / done / archived |
| sort_order | INTEGER DEFAULT 0 | 排序，新建置顶（值越大越靠前） |
| created_task_id | INTEGER NULL | **软关联**：本 Todo 通过 "Run" 派生出的 task id（无 FK 约束） |
| created_at | DATETIME | 创建时间（naive UTC，非弃用的 datetime.now(timezone.utc) 去 tzinfo，与其它模型一致） |
| updated_at | DATETIME | 更新时间 |

索引：`(project_id)` + `(project_id, status, sort_order)`

**状态含义与流转**：
- `open`（待处理，默认）
- `done`（完成）—— 手动勾选，或从 Todo 创建 Task 后自动置为 done
- `archived`（归档，默认隐藏）

允许的流转（均通过 `PATCH status`）：
```
open ⇄ done          （勾选 / 取消勾选）
open/done → archived  （归档按钮）
archived → open       （恢复按钮）
任意 → 永久删除        （DELETE，不可恢复）
```

### `created_task_id` 为什么是软关联

不在 `tasks` 表加列、也不加数据库 FK 约束：
1. SQLite 未启用 FK（`database.py` 无 `PRAGMA foreign_keys=ON`），FK 约束在本项目里本就不生效；
2. 避免对高频写入的 `tasks` 表做 ALTER；
3. `created_task_id` 只是 provenance（溯源），悬空无害。

它取代了「靠 title 匹配 completed task」的脆弱方案：未来做「task 完成 → 提示/自动标记对应 Todo done」时，直接按 `created_task_id` 反查即可，稳定可靠。

## API

```
GET    /api/projects/{project_id}/todos?include_archived=false   列出 todos
POST   /api/projects/{project_id}/todos                          新建 todo
PATCH  /api/projects/{project_id}/todos/{id}                     部分更新（含归档/恢复/排序/关联 task）
DELETE /api/projects/{project_id}/todos/{id}                     永久删除
```

**语义修正（review #1）**：`DELETE` 是**真删除**（`db.delete`），不再是"软删=归档"的语义欺骗。归档改由 `PATCH {"status":"archived"}` 表达，恢复由 `PATCH {"status":"open"}`。

`PATCH` 支持部分更新，可传字段：`title` / `prompt` / `status` / `sort_order` / `created_task_id`（只更新传入的字段）。

排序：列表按 `sort_order DESC, id DESC`。新建时 `sort_order = max(project 内现有 sort_order) + 100`（置顶）。

## 从 Todo 创建 Task 的流程

不新增专用端点，前端直接调用现有 `POST /api/tasks`：

```
用户点 Todo 的 "▶ Run" → 弹窗可改 title/prompt → 提交
  → POST /api/tasks { title, description: prompt, project_id }
      （target_repo 可选，dispatcher 会用 project_id 自动解析，不会 400）
      （provider/model/effort/mode 等走 create_task 默认逻辑）
  → 成功后 best-effort: PATCH todo { status:'done', created_task_id: task.id }
      （失败不阻断跳转——task 已建成，溯源是 nice-to-have）
  → 跳转 #/tasks/chat/{task.id}
```

**幂等（review #4 + 设计闭环）**：创建成功后 Todo 自动置为 `done` 并移出 open 列表，天然避免重复 Run；提交期间弹窗按钮禁用。

## 前端设计

### UI 位置

Todo List 放在 ProjectsPage 每个 Project card 内部，**可折叠**。头部有 open 计数徽标、"Show archived" 切换（有归档项时出现）、"+ Add"。

### 列表项

- 未完成/完成项：勾选圈（open/done）+ title（done 显示删除线）+ prompt 两行预览 + 操作按钮（▶ Run / ✎ Edit / ⌸ Archive）
- 归档项（仅 "Show archived" 打开时可见）：紧凑一行 + `archived` 标签 + ↺ Restore + 🗑 Delete permanently（review 提出的归档死胡同修复）

### 弹窗（TodoModal）

- 新建 / 从 Todo 创建 Task 共用
- Title + Prompt 两个输入
- **Esc 关闭 + 点背景关闭**（review #7），点内容区不关闭（stopPropagation）

### 主题（review #5）

组件全部使用 PR #29 的语义 token 类（`bg-surface` / `text-muted` / `border-border` / `bg-input` / `text-subtle` / `focus:border-focus` / `bg-accent` 等），不用硬编码 `gray-*`。因此本 PR **栈在 #29 之上，应在 #29 之后合并**。

## 级联删除（review #6）

`project_todos.project_id` 声明了 `ON DELETE CASCADE`，但 SQLite 未启用 FK 约束，DB 不会自动级联。故 `projects.py` 删除 project 时**显式删除**其 todos（代码处有注释说明）。此手动删除是必须的，不能因为"已有 CASCADE 声明"而移除。

## 文件

### 新增
| 文件 | 说明 |
|------|------|
| `backend/models/project_todo.py` | ProjectTodo 模型（时区感知时间戳） |
| `backend/schemas/project_todo.py` | Todo CRUD schema |
| `backend/api/project_todos.py` | Todo CRUD API |
| `alembic/versions/f2a3b4c5d6e7_add_project_todos.py` | 建表 migration |
| `frontend/src/components/Projects/ProjectTodoList.tsx` | Todo 列表组件 |

### 修改
| 文件 | 说明 |
|------|------|
| `backend/main.py` | 注册 todo router |
| `backend/api/projects.py` | 删除 project 时级联删 todos |
| `alembic/env.py` | 导入 ProjectTodo 模型 |
| `frontend/src/api/client.ts` | Todo API 方法 + 类型 |
| `frontend/src/pages/ProjectsPage.tsx` | 在 project card 中嵌入 ProjectTodoList |

### 不改
- `tasks` 表 / Task 模型 —— 不加 `source_todo_id`（改为在 `project_todos` 侧记 `created_task_id`）
- `TaskCreate` schema —— 无需额外字段
- 现有 Task 创建逻辑 —— 完全复用

## Alembic（review #10）

`f2a3b4c5d6e7.down_revision` 必须指向当前 head `a2628601782f`（user_skills 系列迁移之后）。最初分支基于旧 base 时写的是 `e7f8a9b0c1d2`，rebase 到最新 main 后会与 `a70ee5479e2e` 同父 → 两个 head → `upgrade head` 失败。已修正。

## 后续可扩展（不在本 PR）

- 「task 完成 → 自动提示/标记对应 Todo done」（用 `created_task_id` 反查，已备好数据基础）
- 拖拽排序 + batch reorder API（`sort_order` 已可通过 PATCH 调整）
- Todo 级别的 provider/model/mode 配置
- 负责人（Team CCM 集成）、标签、批量操作
