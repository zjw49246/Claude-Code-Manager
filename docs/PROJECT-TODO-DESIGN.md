# Project Todo List 设计方案

## 概述

每个 Project 维护一个 Todo List，用于管理待执行的任务 prompt。Todo 可以一键创建 Task 并跳转到 Chat，参数全部使用系统默认值，创建后可修改。

## 数据模型

### 新增表：`project_todos`

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 关联 projects(id)，ON DELETE CASCADE |
| title | VARCHAR(200) NOT NULL | 标题 |
| prompt | TEXT NOT NULL | 任务 prompt |
| status | VARCHAR(20) DEFAULT 'open' | open / done / archived |
| sort_order | INTEGER DEFAULT 0 | 排序（新建置顶，值越大越靠前） |
| created_at | DATETIME | 创建时间 |
| updated_at | DATETIME | 更新时间 |

索引：`(project_id, status, sort_order)`

状态含义：
- `open`：待处理（默认）
- `done`：用户确认完成
- `archived`：归档，默认隐藏

### 不改 tasks 表

MVP 不加 `source_todo_id`。Todo 是 prompt 模板，创建 Task 时复制 title/description 到 task，不建立外键关联。后续如需追踪 todo→task 关系再加。

## API

```
GET    /api/projects/{project_id}/todos          列出 todos（默认只返回 open + done）
POST   /api/projects/{project_id}/todos          新建 todo
PATCH  /api/projects/{project_id}/todos/{id}     更新 todo（title/prompt/status）
DELETE /api/projects/{project_id}/todos/{id}     软删除（status → archived）
```

### 创建 Todo

```json
POST /api/projects/{project_id}/todos
{
  "title": "重构 auth 模块",
  "prompt": "重构 auth 模块，保持现有行为不变，提高代码可读性。"
}
```

### 更新 Todo

```json
PATCH /api/projects/{project_id}/todos/{id}
{
  "title": "重构 auth 模块（含测试）",
  "status": "done"
}
```

支持局部更新，只传需要改的字段。

### 列出 Todos

```json
GET /api/projects/{project_id}/todos?include_archived=false

Response:
[
  {
    "id": 1,
    "title": "重构 auth 模块",
    "prompt": "重构 auth 模块...",
    "status": "open",
    "sort_order": 100,
    "created_at": "2026-06-21T10:00:00",
    "updated_at": "2026-06-21T10:00:00"
  }
]
```

按 `sort_order DESC, id DESC` 排序（新建的在最前面）。

## 从 Todo 创建 Task 的流程

不设专门端点，前端直接调用现有 `POST /api/tasks`：

```
用户点击 Todo 旁边的 "▶ Run" 按钮
  → 前端调用 POST /api/tasks {
      title: todo.title,
      description: todo.prompt,
      project_id: todo.project_id
    }
  → 其他参数（provider/model/effort/mode 等）走现有 create_task 默认逻辑
  → 创建成功后跳转到 Tasks 页面，自动打开该 Task 的 Chat
```

优点：
- 不重复实现 Task 创建逻辑
- 默认参数由 create_task 统一管理
- 用户创建后可以在 Task 详情里修改任何参数

## Task 完成后的 Todo 提示

当存在关联（同 project 下 title 匹配的 completed task），在 Todo 上显示提示：

```
✓ Related task completed. Mark as done?  [Done] [Keep open]
```

MVP 简化：不做自动关联检测。用户手动点 Done 标记完成。

## 前端设计

### UI 位置

Todo List 放在 ProjectsPage 每个 Project card 内部，**可折叠**：

```
┌─ Project: CCM ──────────────────────────────┐
│  test · Color · Git config · Env files · ...│
│                                              │
│  ▼ Todos (3)                      [+ Add]   │
│  ┌──────────────────────────────────────┐   │
│  │ ○ 重构 auth 模块            [▶ Run] │   │
│  │ ○ 修复移动端布局             [▶ Run] │   │
│  │ ✓ 添加单元测试                [Done] │   │
│  └──────────────────────────────────────┘   │
└──────────────────────────────────────────────┘
```

- 默认折叠，点击展开
- `○` = open，`✓` = done
- 点标题可编辑（inline edit）
- "▶ Run" 创建 Task 并跳转 Chat
- "Done" 标记完成
- 右键或长按可 archive

### 新增 Todo 弹窗

点击 "+ Add" 弹出简单 modal：

```
┌─ New Todo ───────────────────┐
│  Title:  [________________]  │
│  Prompt: [                ]  │
│          [                ]  │
│          [                ]  │
│                              │
│         [Cancel] [Create]    │
└──────────────────────────────┘
```

### 组件拆分

```
frontend/src/components/Projects/ProjectTodoList.tsx
```

独立组件，接收 `projectId` prop，内部管理 todo 列表的加载、增删改。不往 ProjectsPage.tsx 堆逻辑。

## 跳转 Chat 的实现

创建 Task 成功后，跳转到 Tasks 页面并打开 Chat：

```typescript
// ProjectTodoList 里创建 task 后
const task = await api.createTask({ title, description, project_id });
window.location.hash = `#/tasks/chat/${task.id}`;
```

利用现有的 URL hash 路由，TasksPage 会解析 `chat/{id}` 并打开对应的 ChatView。

## 文件变动清单

### 新增

| 文件 | 说明 |
|------|------|
| `backend/models/project_todo.py` | ProjectTodo 模型 |
| `backend/api/project_todos.py` | Todo CRUD API |
| `alembic/versions/xxx_add_project_todos.py` | 建表 migration |
| `frontend/src/components/Projects/ProjectTodoList.tsx` | Todo 列表组件 |

### 修改

| 文件 | 说明 |
|------|------|
| `backend/main.py` | 注册 todo router |
| `alembic/env.py` | 导入 ProjectTodo 模型 |
| `frontend/src/api/client.ts` | 添加 todo API 方法 |
| `frontend/src/pages/ProjectsPage.tsx` | 在 project card 中嵌入 ProjectTodoList |

### 不改

- `tasks` 表 / Task 模型 — MVP 不加 `source_todo_id`
- `TaskCreate` schema — 不需要额外字段
- 现有 Task 创建逻辑 — 完全复用

## 后续可扩展

以下不在 MVP 范围，但设计上不阻碍后续添加：

- `source_todo_id` 外键关联 todo→task
- 拖拽排序 + batch reorder API
- Todo 级别的 provider/model/mode 配置
- 负责人（Team CCM 集成）
- 标签、截止时间
- 批量操作（全部 archive、批量创建 task）
