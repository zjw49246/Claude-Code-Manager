# Claude Code 原生命令集成方案

> 调研并规划 `/goal`、`/loop`、`/workflow` 等 Claude Code 原生命令在 Claude Code Manager 中的支持方案。

## TODO 清单

### P0 — 必做

- [ ] **升级 Claude Code 版本**：确保生产环境运行 v2.1.154+（当前开发环境已升至 2.1.161）
- [ ] **Effort 选项扩展**：在 `config.py` 的 `effort_options` 中确认不需要添加 `ultracode`（它不是 CLI `--effort` 的合法值，只能在交互模式中使用；在 `-p` 模式中通过 prompt 关键词触发）
- [ ] **stream_parser 增强**：支持解析 workflow 相关的 `system/task_started`、`system/task_progress`、`system/task_updated`、`system/task_notification` 事件
- [ ] **前端 ChatView 展示 workflow 进度**：当 stream 中出现 `workflow_progress` 数据时，在聊天界面中展示 phase/agent 进度信息

### P1 — 重要

- [ ] **Goal 模式简化**：评估将 `_run_goal_lifecycle` 改为使用原生 `/goal` 前缀透传的可行性（见下方详细方案）
- [ ] **ChatView 斜杠命令透传**：确保用户在对话中输入 `/goal`、`/loop` 等前缀时，消息原样传递给 `claude -p`
- [ ] **Workflow 保存复用**：设计 workflow 模板在系统中的保存和管理机制

### P2 — 增强

- [ ] **Workflow 进度面板**：参考 LoopChatView 的分面板设计，为 workflow 构建独立的进度查看组件
- [ ] **Task 创建 UI 支持 workflow 触发关键词提示**
- [ ] **modelUsage 分析**：从 `result` 事件的 `modelUsage` 中提取多模型消耗（主模型 + 评估器），展示在前端

### 测试清单

- [ ] **Goal 透传测试**：通过 ChatView 发送 `/goal <condition>` 消息，验证 Claude Code 正确进入 goal 循环
- [ ] **Goal stream 事件测试**：验证 goal 模式下 `modelUsage` 中包含评估器模型的 token 消耗
- [ ] **Workflow 触发测试**：通过 ChatView 发送包含 `ultracode` 关键词的消息，验证 workflow 正确启动
- [ ] **Workflow 事件解析测试**：mock `system/task_started`、`task_progress`、`task_notification` 事件，验证 stream_parser 正确解析
- [ ] **Workflow 进度广播测试**：验证 workflow 进度事件通过 WebSocket 正确广播到前端
- [ ] **兼容性测试**：确保旧版 Claude Code（无 /goal、/workflow 支持）不会因新代码而崩溃
- [ ] **effort_level 边界测试**：验证传入非法 effort 值时的错误处理

---

## 1. 调研结论

### 1.1 命令可用性确认

| 命令 | 最低版本要求 | 当前版本 (2.1.161) | `-p` 模式可用 | `stream-json` 输出 |
|------|-------------|-------------------|--------------|-------------------|
| `/goal` | v2.1.139 | ✅ 可用 | ✅ 可用 | ✅ 有事件 |
| `/loop` | 早期版本 | ✅ 可用 | ✅ 可用 | ✅ 有事件 |
| `/workflows` | v2.1.154 | ✅ 可用 | ✅ 可用 | ✅ 有丰富事件 |
| `ultracode` 关键词 | v2.1.154 | ✅ 可用 | ✅ prompt 中包含即可 | ✅ 同 workflow |

**关键发现**：
1. `slash_commands` 列表（来自 `system/init` 事件）包含 `goal`，确认其为原生内置命令
2. `tools` 列表包含 `Workflow`，确认 workflow 作为原生工具可用
3. `ultracode` 不是 CLI `--effort` 的合法值（CLI 只接受 `low/medium/high/xhigh/max`），但在 prompt 文本中包含 `ultracode` 关键词即可触发 workflow 编排
4. `/goal` 在 `-p` 模式下运行时，评估器循环完全在 Claude Code 进程内部完成，一次 `claude -p "/goal ..."` 调用即可运行到 goal 达成或手动中断

### 1.2 stream-json 事件格式（实测数据）

#### /goal 事件

Goal 模式在 stream-json 中**没有**专门的 `goal_evaluation` 事件类型。评估器循环完全在 Claude Code 内部运行。可观测信号为：

```
system/init → assistant (turn 1) → tool_use/tool_result ... → assistant (turn N) → result
```

**观测点**：
- `result.modelUsage` 包含两个模型条目：主模型和评估器模型（例如 `claude-haiku-4-5-20251001`）
- `result.num_turns` 反映 goal 循环的总 turn 数
- `result.total_cost_usd` 包含评估器的消耗

实测示例（主模型 + 评估器分别计费）：
```json
{
  "modelUsage": {
    "claude-haiku-4-5-20251001": {  // 评估器
      "inputTokens": 4123,
      "outputTokens": 161,
      "costUSD": 0.004928
    },
    "claude-haiku-4-5": {            // 主模型
      "inputTokens": 24,
      "outputTokens": 618,
      "costUSD": 0.029310
    }
  }
}
```

#### /workflow (Dynamic Workflows) 事件

Workflow 有丰富的结构化事件：

**1. Workflow 工具调用**（`assistant` 类型）
```json
{
  "type": "assistant",
  "message": {
    "content": [{
      "type": "tool_use",
      "name": "Workflow",
      "input": {
        "script": "export const meta = { name: 'xxx', ... }\n..."
      }
    }]
  }
}
```

**2. `system/task_started`** — workflow 启动
```json
{
  "type": "system",
  "subtype": "task_started",
  "task_id": "wvz0kma14",
  "tool_use_id": "toolu_01Kpkbmhopp7p71bdPtUve5g",
  "description": "Count .txt files in /tmp",
  "task_type": "local_workflow",
  "workflow_name": "count-txt-files"
}
```

**3. `system/task_progress`** — 进度更新（多次）
```json
{
  "type": "system",
  "subtype": "task_progress",
  "task_id": "wvz0kma14",
  "usage": {
    "total_tokens": 9190,
    "tool_uses": 1,
    "duration_ms": 1479
  },
  "workflow_progress": [
    {
      "type": "workflow_phase",
      "index": 1,
      "title": "Count"
    },
    {
      "type": "workflow_agent",
      "index": 1,
      "label": "count-txt-files",
      "phaseIndex": 1,
      "phaseTitle": "Count",
      "agentId": "aa4ee3b5b65804955",
      "model": "claude-haiku-4-5",
      "state": "progress"  // 或 "done"
    }
  ]
}
```

**4. `system/task_updated`** — 状态变更
```json
{
  "type": "system",
  "subtype": "task_updated",
  "task_id": "wvz0kma14",
  "patch": {
    "status": "completed",
    "end_time": 1780558478079
  }
}
```

**5. `system/task_notification`** — 完成通知
```json
{
  "type": "system",
  "subtype": "task_notification",
  "task_id": "wvz0kma14",
  "status": "completed",
  "output_file": "/tmp/.../tasks/wvz0kma14.output",
  "summary": "Dynamic workflow \"Count .txt files in /tmp\" completed",
  "usage": {
    "total_tokens": 9238,
    "tool_uses": 1,
    "duration_ms": 2611
  }
}
```

---

## 2. 方案设计

### 2.1 /goal — 原生命令 vs 自建模式

#### 现状对比

| 维度 | 原生 /goal | 我们的 Goal 模式 (`_run_goal_lifecycle`) |
|------|-----------|----------------------------------------|
| 评估器 | 内置在 Claude Code 中，每 turn 后自动调用 | 我们启动独立的 `claude -p` 子进程做评估 |
| Session 管理 | 自动保持同一 session | 我们手动 `--resume` |
| stream-json 事件 | 无专用事件，通过 modelUsage 可见 | 我们广播 `goal_evaluation` WebSocket 事件 |
| 前端展示 | 无 | TaskList 显示 turn/max_turns/reason |
| 代码量 | 0（传 `/goal` 前缀即可） | ~250 行（dispatcher + evaluator） |
| 可定制性 | 低（评估器模型、行为由 Claude Code 控制） | 高（可定制模型、超时、评估逻辑） |

#### 方案：分场景使用

**场景 A：对话（ChatView）中使用**

当用户在 ChatView 中发送以 `/goal` 开头的消息时，直接原样作为 prompt 传给 `claude -p`。原生 `/goal` 会在子进程内完成整个循环。

- **优点**：零改动即可工作（当前 chat.py 已经是原样透传 `body.message`）
- **注意**：因为整个 goal 循环在一次 `claude -p` 调用中完成，子进程运行时间可能较长；需确保 `task_timeout_seconds` 足够大
- **改动**：无（已经透传）。但需增强前端展示，从 `result.modelUsage` 中提取评估器模型信息

**场景 B：任务调度（Dispatcher）中使用**

对于通过 TaskForm 创建的 `mode="goal"` 任务，有两种选择：

**选项 B1：保持现有实现（推荐短期方案）**
- 现有 `_run_goal_lifecycle` 代码成熟稳定，有完善的进度广播
- 前端已有 goal 进度展示（turn/reason）
- 可定制评估器模型和超时
- 不引入风险

**选项 B2：改为原生 /goal 透传（中期优化）**
- 将 `_build_goal_initial_prompt` 生成的 prompt 加上 `/goal <condition>` 前缀
- 删除 `_run_goal_lifecycle`、`GoalEvaluator`、`_build_goal_followup_prompt` 等代码
- 从 stream-json 的 `result.modelUsage` 中提取评估器信息
- **风险**：失去自定义评估器模型的能力；失去每 turn 的实时进度广播（原生 /goal 不发 goal_evaluation 事件）；子进程长时间运行需要调整超时策略

**推荐**：短期保持 B1，等原生 /goal 的 stream-json 事件更完善后再迁移到 B2。

### 2.2 /loop — 保持现有实现

原生 `/loop` 和我们的 Loop 模式解决的是不同问题：

| 原生 /loop | 我们的 Loop 模式 |
|-----------|-----------------|
| 基于时间间隔的重复执行（如 `/loop 5m check CI`） | 基于 signal file 的分步迭代执行 |
| 适合监控和定期检查 | 适合分解大任务逐步完成 |
| Claude 自己决定何时停止 | 通过 signal file 结构化控制 (continue/done/abort) |
| 无 must_complete 保护 | 支持 must_complete |

**结论**：保持现有 Loop 模式不变。对话（ChatView）中用户输入 `/loop` 前缀消息已经能透传给子进程使用原生功能。

### 2.3 /workflow (Dynamic Workflows) — 新功能集成

#### 核心机制

Dynamic Workflows 是 Claude Code v2.1.154+ 的重大新功能：
- Claude 动态编写一个 JavaScript 编排脚本
- 运行时在后台执行：最多 **16 个并发 subagent**，单次最多 **1000 个 agent**
- 脚本可保存为可复用命令（`.claude/workflows/` 或 `~/.claude/workflows/`）
- 触发方式：prompt 中包含 `ultracode` 关键词，或自然语言"use a workflow"
- 支持对抗验证：subagent 互相审查
- 可暂停、恢复；进度通过 `/workflows` 查看

#### 集成方案

**阶段一：透传支持（最小改动）**

Workflow 在 `-p` 模式下可以自动触发。只需确保：

1. **stream_parser.py 增强**：解析 workflow 相关的 system 事件（当前的 `system_event` 处理器把所有 system 事件的 content 设为 subtype 字符串，丢失了丰富的结构化数据）

   需要新增的事件处理：
   ```python
   # 当前代码（line 49-58）把所有非 init 的 system 事件统一处理为 content=subtype
   # 需要对以下 subtype 保留完整数据：
   # - task_started: workflow 启动
   # - task_progress: workflow 进度（含 workflow_progress 数组）
   # - task_updated: workflow 状态变更
   # - task_notification: workflow 完成通知
   ```

2. **instance_manager.py 增强**：对 workflow 事件，广播完整的结构化数据而不仅仅是 subtype 字符串

3. **前端 ChatView**：检测 workflow 相关事件，在聊天界面中展示进度信息

**触发方式**：
- 用户在 ChatView 中发送包含 `ultracode` 关键词的消息
- 用户在 ChatView 中直接说 "use a workflow" / "run a workflow"
- 消息原样传给 `claude -p`，Claude 自动调用 Workflow 工具

**阶段二：任务级集成**

1. **Task 创建支持 workflow 触发**
   - 在 TaskForm 中添加可选的 "使用 Workflow 编排" 勾选框
   - 勾选后，dispatcher 在构建 prompt 时自动添加 `ultracode:` 前缀
   - 不需要新的 `mode`，因为 workflow 编排由 Claude 自主决定

2. **Workflow 保存和复用**
   - Claude Code 原生支持将 workflow 脚本保存到 `.claude/workflows/` 目录
   - 在 `-p` 模式中，可以直接在 prompt 中引用已保存的 workflow：`/workflow-name`
   - 前端可以列出已保存的 workflow，让用户选择复用
   - 需要后端 API 来读取 `.claude/workflows/` 和 `~/.claude/workflows/` 目录

3. **Workflow 进度面板**
   - 参考 LoopChatView 的分面板设计
   - 按 phase 分组展示 agent 状态（progress/done）
   - 显示 token 消耗和时长

#### ultracode 与 effort_level 的关系

**重要发现**：`ultracode` 不是 CLI `--effort` 参数的合法值。

```
$ claude -p "test" --effort ultracode
Warning: Unknown --effort value 'ultracode' — ignoring it and using the default effort.
Valid values: low, medium, high, xhigh, max.
```

在交互模式中，`/effort ultracode` 会设置两件事：
1. effort 级别为 `xhigh`
2. 启用自动 workflow 编排

在 `-p` 模式中，等效的做法是：
- CLI 传 `--effort xhigh`
- prompt 中包含 `ultracode` 关键词

**结论**：不要在 `effort_options` 中添加 `ultracode`。如果想在系统中支持 "ultracode 模式"，应该：
- 在 Task 或 Instance 上添加一个单独的 `use_workflow: bool` 字段
- 当 `use_workflow=True` 时，dispatcher 在 prompt 开头自动添加 `ultracode:` 前缀，并将 `--effort` 设为 `xhigh`

---

## 3. 实现细节

### 3.1 stream_parser.py 改动

当前 `stream_parser.py`（line 49-58）把所有非 init 的 system 事件统一处理：

```python
elif event_type == "system":
    subtype = data.get("subtype", "system")
    _SKIP_SUBTYPES = {"thinking_tokens", "token_usage", "api_request", "api_response"}
    if subtype in _SKIP_SUBTYPES:
        return []
    event = _base_event()
    event["event_type"] = "system_event"
    event["content"] = subtype  # ← 丢失了结构化数据
    return [event]
```

需要增加对 workflow 事件的特殊处理：

```python
elif event_type == "system":
    subtype = data.get("subtype", "system")
    _SKIP_SUBTYPES = {"thinking_tokens", "token_usage", "api_request", "api_response"}
    if subtype in _SKIP_SUBTYPES:
        return []

    # Workflow 相关事件：保留完整结构化数据
    _WORKFLOW_SUBTYPES = {"task_started", "task_progress", "task_updated", "task_notification"}
    if subtype in _WORKFLOW_SUBTYPES:
        event = _base_event()
        event["event_type"] = "system_event"
        event["content"] = subtype
        # 在 raw_json 中已有完整数据，额外提取关键字段供前端使用
        event["workflow_data"] = {
            "subtype": subtype,
            "task_id": data.get("task_id"),
            "workflow_name": data.get("workflow_name"),
            "task_type": data.get("task_type"),
            "status": data.get("status") or (data.get("patch", {}).get("status")),
            "usage": data.get("usage"),
            "workflow_progress": data.get("workflow_progress"),
            "summary": data.get("summary") or data.get("description"),
        }
        return [event]

    event = _base_event()
    event["event_type"] = "system_event"
    event["content"] = subtype
    return [event]
```

### 3.2 instance_manager.py 改动

在 `_process_event` 方法中，当事件包含 `workflow_data` 时，将其包含在 WebSocket 广播中：

```python
broadcast_data = {k: v for k, v in event.items() if k != "raw_json"}
# workflow_data 已在 broadcast_data 中，前端可直接使用
```

（当前代码已经这样做了，因为 broadcast_data 排除的只是 raw_json）

### 3.3 前端 ChatView 展示

在 ChatView 中检测 `workflow_data` 字段，展示 workflow 进度：

```tsx
// 当收到 system_event 且有 workflow_data 时
if (msg.event_type === 'system_event' && msg.workflow_data) {
  const wd = msg.workflow_data;
  if (wd.subtype === 'task_started') {
    // 显示 "🔄 Workflow 'xxx' started"
  } else if (wd.subtype === 'task_progress' && wd.workflow_progress) {
    // 显示 phase/agent 进度
  } else if (wd.subtype === 'task_notification') {
    // 显示 "✓ Workflow 'xxx' completed (tokens, duration)"
  }
}
```

### 3.4 Workflow 保存和复用 API

新增后端 API 读取已保存的 workflow：

```python
# GET /api/workflows — 列出所有已保存的 workflow
# 扫描 .claude/workflows/ 和 ~/.claude/workflows/
# 返回 [{name, description, scope: "project"|"user", path}]
```

前端在 TaskForm 或 ChatView 中提供 workflow 选择器，用户选择后在 prompt 中自动添加 `/<workflow-name>` 前缀。

---

## 4. 对话模式下的斜杠命令透传

### 当前行为

ChatView → `POST /api/tasks/{id}/chat` → `chat.py` 将 `body.message` 原样拼入 prompt → `instance_manager.launch()` 传给 `claude -p`。

**已经是透传的。** 用户在 ChatView 中输入 `/goal all tests pass` 或 `ultracode: audit all endpoints`，消息会原样传给 Claude Code 子进程。

### 需要注意的问题

1. **超时**：`/goal` 可能导致子进程运行很长时间（多 turn 循环在一次调用中完成），需要 `task_timeout_seconds` 足够大或动态调整
2. **中断**：用户需要能够通过 Interrupt 按钮中断正在运行的 goal/workflow
3. **进度感知**：前端需要从 stream 事件中识别出当前正在进行 goal 循环或 workflow 编排，给用户适当的视觉反馈

---

## 5. 风险和限制

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 原生 /goal 没有独立的 stream 评估事件 | 前端无法实时显示 goal 评估进度 | 保持自建 goal 模式用于 Task 调度；对话中用原生 /goal 时接受此限制 |
| Workflow 可能消耗大量 token | 费用不可控 | 前端提示 workflow 可能产生高消耗；在 task_progress 事件中实时显示 token 使用量 |
| `ultracode` 关键词可能被意外触发 | 用户不经意间触发 workflow | 前端可以检测 prompt 中的 `ultracode` 关键词，弹出确认提示 |
| Claude Code 版本降级 | /goal 或 /workflow 不可用 | 在 system/init 事件中检查 slash_commands 列表，不包含则禁用相关 UI |

---

## 6. 参考资料

- [Claude Code /goal 官方文档](https://code.claude.com/docs/en/goal)
- [Claude Code Dynamic Workflows 官方文档](https://code.claude.com/docs/en/workflows)
- [Introducing dynamic workflows in Claude Code (Blog)](https://claude.com/blog/introducing-dynamic-workflows-in-claude-code)
- [A harness for every task: dynamic workflows in Claude Code (Blog)](https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code)
