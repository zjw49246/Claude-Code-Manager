# 子 Agent 显示与状态问题修复方案

## 问题描述

### 问题 1：子 agent 运行时前端不显示

当 CC session 调用内置 Agent tool（尤其是 `run_in_background: true`）时，CCM 的子 agent 面板未能捕捉并显示。用户看不到有子 agent 在运行。

**根因**：PTY 的 `SubagentTracker` 通过 JSONL 的 `tool_use` block 检测子 agent。检测链中某个环节可能断裂：

```
JSONL tool_use(name=Agent) 
  → SubagentTracker.note_tool_use() 
  → SUBAGENT_SPAWN 事件
  → CCM _upsert_native_sub_agent() 
  → DB SubAgentSession 
  → WebSocket 广播 
  → 前端 SubAgentIndicator
```

可能断裂点：
- background agent 的 `tool_use` 在 JSONL 中格式不同
- JSONL 轮询时机——spawn 事件在 turn 结束后才被轮询到
- `_process_event` 异常未捕获

### 问题 2：子 agent 内容不可见

当前只显示状态（运行中/完成/失败），无法看到子 agent 正在做什么。

**根因**：子 agent 的 transcript 存在 `<session-dir>/subagents/agent-<id>.jsonl`，但 PTY 只检查文件大小变化（`transcripts_grew()`），不读取内容。CCM 不把 transcript 内容传给前端。

### 问题 3：子 agent 运行中 task 状态变 completed

CC 主 turn 调用 Agent tool 后立刻发 `system/turn_duration`，PTY 认为 turn 结束，CCM 把 task 标记为 completed。但 background agent 还在跑。agent 完成后触发新 turn 回消息——此时 task 已经 completed。

**根因**：turn 结束判断（`is_response_complete`）只看 `turn_duration` 哨兵，不检查 `has_pending_subagents`。

```python
# session.py 当前逻辑：
if is_response_complete(raw):  # 只看 turn_duration
    break  # 不管子 agent 是否在跑
```

`has_pending_subagents` 只防止 session idle eviction，不阻止 turn 完成或 task completed。

## 修复方案

### 修复 1：子 agent 显示

**1a. 确保所有 Agent tool_use 被捕获**

```python
# subagents.py - 扩展检测逻辑
AGENT_TOOL_NAMES = frozenset({"Agent", "Task"})

def note_tool_use(self, block: dict) -> dict | None:
    name = block.get("name", "")
    if name not in AGENT_TOOL_NAMES:
        return None
    tool_input = block.get("input", {})
    return {
        "tool_use_id": block.get("id"),
        "kind": "native-agent",
        "description": tool_input.get("description", ""),
        "agent_type": tool_input.get("subagent_type", "general-purpose"),
        "background": tool_input.get("run_in_background", False),  # 新增
    }
```

**1b. 增加 JSONL 轮询频率保证**

在 `_consume_output` 循环中确保即使 turn 快速结束，也先处理完当前 batch 的所有事件再判断 turn 完成。

### 修复 2：子 agent 内容展示

**2a. 读取子 agent transcript 内容**

```python
# subagents.py 新增方法
def read_transcript(self, tool_use_id: str) -> list[dict]:
    """读取子 agent 的 transcript 内容。"""
    d = self._subagents_dir
    if not d:
        return []
    transcript = os.path.join(d, f"agent-{tool_use_id}.jsonl")
    if not os.path.exists(transcript):
        return []
    events = []
    with open(transcript) as f:
        for line in f:
            try:
                raw = json.loads(line)
                # 提取 assistant message 和 tool_use
                if raw.get("type") == "assistant":
                    for block in raw.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            events.append({"type": "message", "text": block["text"]})
                        elif block.get("type") == "tool_use":
                            events.append({"type": "tool_use", "name": block["name"]})
            except json.JSONDecodeError:
                continue
    return events
```

**2b. 定期广播子 agent 进度**

```python
# instance_manager.py
# 在 _consume_output 循环中增加子 agent transcript 轮询
async def _poll_subagent_transcripts(self, task_id, tracker):
    """定期读取子 agent transcript 并广播进度。"""
    for sa_id, info in tracker.pending.items():
        events = tracker.read_transcript(info["tool_use_id"])
        if events:
            latest = events[-1]
            await self._upsert_native_sub_agent(task_id, "subagent_progress", {
                "tool_use_id": info["tool_use_id"],
                "summary": latest.get("text", "")[:500],
            })
```

**2c. 前端 MonitorPanel 显示子 agent 详情**

当前 MonitorPanel 只显示 `last_summary`。增加展开功能显示子 agent 的消息流（thinking、tool_use、message）。

### 修复 3：状态修复——子 agent 运行中不标记 completed

**方案 A（PTY 层）：turn 结束时检查 pending sub-agents**

```python
# session.py
async def _send_prompt_inner(self, text, timeout, ...):
    ...
    for raw in messages:
        if is_response_complete(raw):
            # 同步 Agent/Task tool 还在 pending 时不结束 turn
            if self._tracker.has_pending_sync_agents:
                continue  # 等 tool_result 到达
            break
```

需要区分同步 agent（Agent/Task，阻塞 turn）和后台 agent（Monitor，不阻塞）：
- 同步 Agent/Task 的 tool_result 到达前不应结束 turn（但 CC 不会在中间发 turn_duration）
- Background agent 运行中 turn 可以结束，但 task 不应标记 completed

**方案 B（CCM 层）：task completed 时检查活跃子 agent**

```python
# adapters/ccm.py - on_exit 或 status_change 时
async def _check_before_complete(self, task_id):
    """task 标记 completed 前检查是否有活跃的 native sub-agent。"""
    async with self.db_factory() as db:
        active_count = (await db.execute(
            select(func.count()).select_from(SubAgentSession).where(
                SubAgentSession.task_id == task_id,
                SubAgentSession.source == "native",
                SubAgentSession.status == "running",
            )
        )).scalar() or 0
    
    if active_count > 0:
        # 不标记 completed，保持 executing
        # 子 agent 全部完成后再标记 completed
        return False
    return True
```

**推荐方案 B**——在 CCM 层面控制，不改 PTY 的 turn 判定逻辑（改 PTY 影响面太大）。

### 修复 4：子 agent 完成后的消息处理

当 background agent 完成后 CC 自动开始新 turn 回复结果。此时 task 状态应该从 completed 回到 executing，处理完后再变 completed。

```python
# 当收到新的 assistant message 且 task 是 completed 状态时：
if task.status == "completed" and event_type in ("message", "tool_use"):
    task.status = "executing"
    await db.commit()
    # 广播状态变更
```

## 实施优先级

1. **P0**：修复 3——状态不应该在子 agent 运行中变 completed（影响用户体验最大）
2. **P1**：修复 1——确保子 agent 被显示（用户知道有子 agent 在跑）
3. **P2**：修复 4——子 agent 完成后的消息正确触发状态变更
4. **P3**：修复 2——子 agent 内容展示（增强功能，可后续）

## 与 Skills 系统的关系

子 agent 系统是独立于 skills 的运行时机制。但有交叉点：
- 子 agent 可能在执行 skill 相关的任务（如 $monitor 创建的监控子 agent）
- 子 agent 的失败可以触发 skill 进化（即时环的 evolve_on_failure）
- 子 agent 的使用模式可以被 Distill 分析并生成新 skill

修复优先级高于 skills 系统开发——因为子 agent 状态问题影响现有功能的正确性。
