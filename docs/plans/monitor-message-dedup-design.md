# Monitor 消息去重与标记设计

## 当前问题

Monitor 子 Agent 向主 session 汇报时，同一条消息显示两遍甚至三遍：

1. **通知卡片**：`monitor_check` WebSocket 事件 → 前端 ChatView 插入 `system_event` 灰色卡片
2. **用户消息气泡**：`is_important=True` 时 `dispatcher.enqueue_message()` → `user_message` 事件 → 聊天气泡
3. **主 Agent 回复**：主 Agent 收到注入 prompt 后回复用户"简要转达监控结果"

用户在聊天界面看到同一条 Monitor 信息以三种不同视觉形式出现，体验混乱。

---

## 根因

两条独立路径（WebSocket 广播 + `enqueue_message`）同时产生消息，内容本质相同：

- **路径 A**（通知卡片）：Monitor Agent 调用 `report_status` MCP 工具 → HTTP `POST /api/tasks/{id}/monitor-sessions/{sid}/checks` → DB 写入 MonitorCheck → WebSocket 广播 `monitor_check` 事件 → 前端 ChatView 渲染为灰色 `system_event` 卡片
- **路径 B**（聊天注入）：同一个 API handler 在 `is_important=True` 时调用 `dispatcher.enqueue_message()` → 消息入 per-task 队列 → consumer `--resume` 主 session → 产生 `user_message` 事件 → 前端渲染为用户消息气泡 → 主 Agent 收到后回复转达

两条路径各自独立，没有互相感知，导致同一信息重复展示。

---

## 数据流图

### 当前（重复展示）

```
Monitor Agent
  │
  ▼
report_status(summary, is_important=True)
  │
  ▼
POST /checks API handler
  │
  ├──────────────────────────────────┐
  │                                  │
  ▼                                  ▼
路径 A: WS 广播                   路径 B: enqueue_message()
monitor_check 事件                    │
  │                                  ▼
  ▼                              per-task queue
ChatView 插入                        │
system_event 灰色卡片               ▼
  │                              --resume 主 session
  │                                  │
  │                              ┌───┴───┐
  │                              ▼       ▼
  │                          user_message  主 Agent 回复
  │                          气泡      "监控发现..."
  │                              │       │
  ▼                              ▼       ▼
╔═══════════════════════════════════════════════╗
║  聊天界面同时出现 3 条：                        ║
║  [灰色卡片] Monitor 报告: xxx                  ║
║  [用户气泡] [Monitor] xxx                      ║
║  [助手气泡] 监控发现 xxx，我来处理...            ║
╚═══════════════════════════════════════════════╝
```

### 修复后（单一展示路径）

```
Monitor Agent
  │
  ▼
report_status(summary, is_important)
  │
  ▼
POST /checks API handler
  │
  ├── is_important=False ──────────────────┐
  │                                        │
  │                                        ▼
  │                                   WS 广播 monitor_check
  │                                   chat_injected=False
  │                                        │
  │                                        ▼
  │                                   仅更新 MonitorPanel
  │                                   不插入聊天流
  │
  └── is_important=True ──────────────────┐
                                          │
                                    ┌─────┴─────┐
                                    ▼           ▼
                              enqueue_message  WS 广播 monitor_check
                              (带 monitor_     chat_injected=True
                               session_id)         │
                                    │              ▼
                                    ▼         前端：跳过聊天流插入
                              --resume           仅更新 MonitorPanel
                              主 session
                                    │
                                    ▼
                              user_message 气泡
                              [Monitor: <monitor名>] summary
                                    │
                                    ▼
                              主 Agent 根据内容
                              决定是否需要行动
                                    │
                                    ▼
╔═══════════════════════════════════════════════╗
║  聊天界面只出现 1-2 条：                        ║
║  [用户气泡] [Monitor: 编译监控] 构建失败...      ║
║  [助手气泡] 我来修复这个问题...（可选）          ║
╚═══════════════════════════════════════════════╝
```

---

## 修复方案

### 规则总结

| 场景 | 聊天流 | MonitorPanel | WS 广播 |
|------|--------|-------------|---------|
| `is_important=False` | 不插入 | 更新 | `monitor_check` + `chat_injected=False` |
| `is_important=True` | `user_message` 注入（带 `[Monitor]` 标记） | 更新 | `monitor_check` + `chat_injected=True` |
| 历史 `system_event`（兼容） | 降低视觉权重（浅灰 + 小字） | 不变 | 不变 |

### 核心变更

1. **`monitor_check` 广播增加 `chat_injected` 字段**：后端在广播 `monitor_check` 事件时，根据是否同时执行了 `enqueue_message()`，设置 `chat_injected=True/False`。前端据此决定是否将该事件插入聊天流。

2. **`enqueue_message()` 标记来源**：`QueuedMessage` 新增 `monitor_session_id` 字段，dispatcher 在消费时将其附加到 `user_message` 事件中，前端可据此渲染 `[Monitor]` 标记。

3. **前端 ChatView 过滤逻辑**：收到 `monitor_check` 事件时，检查 `chat_injected`：
   - `True`：不插入聊天流（对应的 `user_message` 会单独到达）
   - `False`：不插入聊天流（不重要，仅更新 MonitorPanel）
   - 字段缺失（历史数据）：按旧逻辑插入，但降低视觉权重

4. **历史消息兼容**：旧格式的 `[Monitor]` system_event 仍然渲染，但改用更低视觉权重的样式（浅灰色背景、小字体、左侧灰色竖线），避免与正式聊天消息混淆。

---

## 改动清单

### `backend/api/monitor.py`

**变更**：`POST /tasks/{task_id}/monitor-sessions/{session_id}/checks` handler

```python
# 当前代码（简化）：
async def create_check(...):
    check = MonitorCheck(...)
    db.add(check)
    await db.commit()
    # 路径 A：始终广播
    await broadcaster.broadcast(f"task:{task_id}", {
        "type": "monitor_check",
        "data": check_data
    })
    # 路径 B：is_important 时注入
    if check.is_important:
        await dispatcher.enqueue_message(task_id, summary)

# 修复后：
async def create_check(...):
    check = MonitorCheck(...)
    db.add(check)
    await db.commit()

    chat_injected = False
    if check.is_important:
        # 路径 B：注入主 session，带 monitor_session_id
        await dispatcher.enqueue_message(
            task_id,
            f"[Monitor: {session.name}] {summary}",
            monitor_session_id=session.id
        )
        chat_injected = True

    # 路径 A：始终广播，附加 chat_injected 标记
    await broadcaster.broadcast(f"task:{task_id}", {
        "type": "monitor_check",
        "data": {**check_data, "chat_injected": chat_injected}
    })
```

**要点**：
- `enqueue_message` 先于广播执行，确保 `chat_injected` 准确反映实际行为
- `summary` 前缀从通用 `[Monitor]` 改为 `[Monitor: {name}]`，区分多个 Monitor

---

### `backend/services/dispatcher.py`

**变更 1**：`QueuedMessage` dataclass 新增 `monitor_session_id`

```python
@dataclass
class QueuedMessage:
    task_id: int
    content: str
    priority: int = 5
    monitor_session_id: int | None = None  # 新增：来源 Monitor session ID
```

**变更 2**：`enqueue_message()` 方法签名扩展

```python
async def enqueue_message(
    self,
    task_id: int,
    content: str,
    priority: int = 5,
    monitor_session_id: int | None = None,  # 新增
) -> None:
    msg = QueuedMessage(
        task_id=task_id,
        content=content,
        priority=priority,
        monitor_session_id=monitor_session_id,
    )
    await self._task_queues[task_id].put(msg)
```

**变更 3**：`_task_queue_consumer` 消费时附加元数据

在 consumer 发送 `user_message` 广播时，如果 `msg.monitor_session_id` 非空，在事件 payload 中附加：

```python
event_data = {
    "type": "user_message",
    "content": msg.content,
}
if msg.monitor_session_id:
    event_data["monitor_session_id"] = msg.monitor_session_id
    event_data["source"] = "monitor"

await broadcaster.broadcast(f"task:{task_id}", event_data)
```

前端据 `source: "monitor"` 渲染带标记的气泡。

---

### `frontend/src/components/Chat/ChatView.tsx`

**变更 1**：`monitor_check` 事件处理逻辑

```typescript
// 当前代码（简化）：
case 'monitor_check':
  // 插入灰色卡片到聊天流
  setChatMessages(prev => [...prev, {
    type: 'system_event',
    subtype: 'monitor_check',
    content: data.summary,
  }]);
  break;

// 修复后：
case 'monitor_check':
  // 更新 MonitorPanel（始终执行）
  updateMonitorPanel(data);

  // 聊天流：仅在 chat_injected 字段缺失时插入（历史兼容）
  if (data.chat_injected === undefined) {
    // 历史数据：降低视觉权重但仍显示
    setChatMessages(prev => [...prev, {
      type: 'system_event',
      subtype: 'monitor_check',
      content: data.summary,
      style: 'muted',  // 新增：触发低权重样式
    }]);
  }
  // chat_injected=true/false 都不插入聊天流
  break;
```

**变更 2**：`user_message` 事件增加 Monitor 来源标记渲染

```typescript
// 渲染 user_message 时，检查 source 字段
if (message.source === 'monitor') {
  // 渲染带 [Monitor] 标记的消息气泡
  // 使用区分色（如蓝紫色左边框）标识来自 Monitor 的注入消息
  return <MonitorMessageBubble content={message.content} />;
}
```

**变更 3**：历史 `system_event` 降低视觉权重

对 `style: 'muted'` 或旧格式的 monitor system_event，应用以下样式：

```css
/* 旧 Monitor 通知卡片 — 降低视觉权重 */
.monitor-check-muted {
  @apply text-xs text-gray-400 border-l-2 border-gray-300 pl-2 py-1 my-0.5;
}
```

相比当前的灰色卡片（`text-sm bg-gray-100 p-3 rounded`），视觉权重显著降低，不会与正式消息混淆。

---

## 迁移与兼容性

- **WebSocket 协议**：`monitor_check` 事件新增 `chat_injected` 字段，旧前端不识别该字段时按原有逻辑处理（字段缺失 = 插入），不会 break
- **数据库**：无 schema 变更，`chat_injected` 仅存在于 WebSocket 事件中
- **历史消息**：DB 中已有的 `system_event` 记录不受影响，前端对缺少 `style` 字段的旧记录按默认（muted）处理
- **MonitorPanel**：不受影响，始终展示所有 checks（重要和非重要）

---

## 测试要点

1. **is_important=False**：验证聊天流无新消息，MonitorPanel 正常更新
2. **is_important=True**：验证聊天流仅出现一条 `[Monitor: xxx]` 用户气泡 + 主 Agent 回复，无灰色卡片
3. **多 Monitor 并发**：不同 Monitor 的消息应携带各自名称，不混淆
4. **历史消息**：旧格式 system_event 仍可渲染，视觉权重降低
5. **前端重连**：WebSocket 断线重连后，backfill 的历史消息正确处理 `chat_injected` 字段（缺失时走兼容路径）
