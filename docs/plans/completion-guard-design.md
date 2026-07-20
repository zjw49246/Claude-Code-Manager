# 任务完成校验与自动催促设计

> 状态：设计稿，尚未实施。
>
> 本文定义 Task 级“完成校验与自动催促”（下文简称 Completion Guard）的产品语义、状态机、消息协议、并发规则和建议改动。它适用于 Claude 与 Codex provider，并与现有 per-task 消息队列、AskUserQuestion、Goal/Loop、分布式 Worker 和任务状态广播机制衔接。

## 1. 背景

当前任务的一次 Claude/Codex turn 正常退出后，多条执行路径会直接把 Task 标记为 `completed`。但“进程正常结束”只代表本轮没有进程级错误，并不必然代表用户的要求已经完整实现。例如：

- Agent 只完成了部分代码便总结退出；
- Agent 遗漏测试、迁移、构建或兼容性要求；
- Agent 误把可以自行完成的工作交还给用户；
- Agent 正在等待一个确实只能由用户做出的选择；
- Agent 声称完成，但最近的消息已经追加了新的要求。

Completion Guard 的目标是把“进程成功退出”和“任务真正完成”分离。开启后，正常退出只产生一个**候选完成点**；系统必须经过独立校验，并根据下一步应该由谁行动来决定：

1. 让原 session 做最后自检；
2. 向原 session 注入具体催促并继续工作；
3. 只提醒用户回答问题，不触发原 session；
4. 双方确认后才把 Task 标记为 `completed`。

## 2. 设计目标

### 2.1 必须满足

1. Completion Guard 是 Task 的动态设置，任务创建后、执行中、后续对话中均可随时开启或关闭。
2. 设置对 Claude、Codex 使用相同的产品语义。
3. 开启时，任何自然成功路径都不能绕过完成守门直接写 `completed`。
4. 独立校验 Agent 与原 session 必须分别作出确认；最终共识由 CCM 系统生成，不能由任一 Agent 自行填写。
5. 未完成时必须判断 `next_actor`：原 Agent可以继续时才注入催促；必须由用户回答时只通知用户。
6. 所有判断必须绑定设置版本和任务快照，过期结果不得生效。
7. 同一个原 session 不得出现并发 resume。
8. 达到催促上限、校验异常或存在冲突时必须 fail closed：不误标完成。
9. 每轮校验、催促、用户等待和最终决策可审计、可在前端解释。

### 2.2 非目标

1. 不承诺两个模型的判断在逻辑上绝对正确；系统能严格保证的是“未满足协议定义的双确认就不进入 completed”。
2. 校验 Agent 不负责修改代码，也不代替原 Agent工作。
3. 校验 Agent 不得代替用户回答业务选择、授权、凭证或破坏性操作确认。
4. Completion Guard 不代替现有的进程失败重试、瞬时过载重试、账号池轮换、上下文压缩或人工取消。

## 3. 核心术语

| 术语 | 含义 |
|------|------|
| 原 session | 实际执行用户任务的 Claude/Codex session/thread |
| 校验 Agent | 独立、短生命周期、只负责判断任务状态的 evaluator |
| 候选完成点 | 原 session 的一轮执行正常结束，但 Task 尚未真正 completed |
| 原 Agent 声明 | 原 session 对当前任务状态的结构化声明 |
| 双确认 | 校验 Agent 与原 session 都针对有效的最新状态确认完成 |
| 下一行动者 | 未完成时应该采取下一步行动的一方：`original_agent` 或 `user` |
| 设置版本 | Completion Guard 每次开关变化时递增的版本号 |
| 校验快照 | 某轮校验使用的任务输入、对话、执行证据及版本标识 |

## 4. 产品语义

### 4.1 开关名称

创建任务和任务详情中提供：

```text
完成校验与自动催促  [开 / 关]
```

建议 API/数据库字段命名为：

```text
completion_guard_enabled
```

不应放入 `enabled_skills`。它属于 Dispatcher 的任务生命周期策略，不是 Claude MCP Skill；Codex 也不支持现有 skills-MCP 注入路径。

### 4.2 动态生效原则

开关是 Task 的实时策略，而不是启动时复制到进程中的一次性参数。Dispatcher 在每个关键边界重新读取数据库，不长期依赖启动时缓存的 Task 对象。

必须重新读取的边界包括：

1. 原 session 的 turn 结束时；
2. 校验 Agent 启动前；
3. 校验 Agent 返回后；
4. 自检/催促消息入队前；
5. 队列消息真正 resume 原 session 前；
6. 原 session 再次结束时；
7. 最终提交 `completed` 前。

### 4.3 已完成、失败、取消任务上的设置

- 已完成 Task 可以修改开关，但修改本身不重开任务；之后用户发送新消息触发新 turn 时使用最新设置。
- failed Task 修改后在下一次 retry/resume 生效。
- cancelled Task 修改后在下一次恢复执行时生效。
- plan_review 阶段只保存设置，在实际执行到候选完成点时生效。

## 5. 判断模型：状态与下一行动者

Completion Guard 不能只输出 `complete=true/false`。未完成时必须判断下一步由谁行动。

校验结论使用以下组合：

### 5.1 `complete + none`

校验 Agent 认为用户当前有效要求已经完成。此时还不能直接完成 Task，必须让原 session 做一次最终自检。

### 5.2 `incomplete + original_agent`

任务未完成，但所需信息、权限和工具均已具备，原 Agent 可以继续。例如：

- 少写了测试；
- 没有运行构建；
- 漏了数据库迁移；
- 仓库中已有答案但 Agent 没有查找；
- 技术细节可以依据当前项目约定自主决定。

系统向原 session 注入带具体缺失项的催促消息。

### 5.3 `waiting_user + user`

任务未完成，且下一步确实必须由用户提供信息、做出选择或授权。例如：

- 多种产品方案会产生明显不同的外部行为；
- 需要确认不可逆/破坏性操作；
- 缺少无法从上下文或仓库推断的业务规则；
- 需要用户提供凭证、账号或外部系统信息；
- 需要扩大任务权限或范围。

系统只提醒用户，不 resume 原 session。

### 5.4 `uncertain`

校验超时、格式解析失败、证据冲突或 evaluator 无法可靠判断。按未完成处理，不得自动放行。

## 6. 总体状态机

```text
pending / in_progress
        │
        ▼
    executing
        │ 原 turn 正常结束
        ▼
 candidate_completion
        │
        ├── Guard 已关闭 ───────────────► 普通完成规则
        │
        └── Guard 已开启
                │
                ▼
            reviewing
                │
       ┌────────┼──────────────┐
       │        │              │
       ▼        ▼              ▼
    complete  incomplete    waiting_user
       │      next=agent     next=user
       │        │              │
       ▼        ▼              ▼
 self_checking continuing   waiting_user
       │        │              │
       └────┬───┘              │ 用户回答
            │ 原 turn 结束      │
            ▼                  ▼
         reviewing ◄──────── executing
            │
            ▼
  双方确认且版本仍有效
            │
            ▼
        completed

超过轮数 / 无法恢复
            │
            ▼
      needs_attention
```

推荐新增 Task 展示状态 `verifying` 和 `waiting_user`；内部更细阶段保存在 `completion_guard_state`，避免把所有内部步骤扩散成顶层 Task.status。

## 7. 消息与结果协议

### 7.1 原 session 的 turn outcome

Guard 开启时，在任务 prompt 中追加 provider-neutral 的协议说明，要求原 session 在准备结束工作时输出：

```xml
<ccm_turn_outcome>
{
  "state": "claimed_complete",
  "summary": "已完成任务要求",
  "evidence": ["后端测试通过", "前端构建通过"],
  "remaining": [],
  "question": null
}
</ccm_turn_outcome>
```

`state` 允许：

- `claimed_complete`
- `incomplete`
- `waiting_user`

等待用户时：

```xml
<ccm_turn_outcome>
{
  "state": "waiting_user",
  "summary": "需要用户选择迁移策略",
  "evidence": [],
  "remaining": ["确定旧数据的处理方式"],
  "question": {
    "text": "保留旧数据并迁移，还是清空后重新导入？",
    "options": ["保留并迁移", "清空后导入"],
    "why_user_required": "该选择会改变已有用户数据"
  }
}
</ccm_turn_outcome>
```

该声明只是信号，不直接决定 Task 状态。缺失或解析失败时，校验 Agent仍会根据上下文判断；系统不得因缺失格式而默认完成。

### 7.2 校验 Agent 输出

校验 Agent 必须只输出一个 JSON 对象：

```json
{
  "verdict": "incomplete",
  "next_actor": "original_agent",
  "reason": "核心功能已实现，但缺少回归测试",
  "missing_items": [
    "补充关闭 Guard 时丢弃过期 evaluator 结果的测试"
  ],
  "user_question": null,
  "evidence": [
    "最近一次回复没有测试结果"
  ]
}
```

允许值：

```text
verdict: complete | incomplete | waiting_user | uncertain
next_actor: none | original_agent | user
```

服务端必须校验组合合法性：

- `complete` 只能配 `none`；
- `incomplete` 只能配 `original_agent`；
- `waiting_user` 只能配 `user`；
- `uncertain` 不产生自动完成，可进入重试或人工处理。

`snapshot_revision`、`guard_version` 等可信元数据由服务端包裹保存，不信任模型自行回填。

### 7.3 原 session 最终自检输出

当校验 Agent 判断完成后，系统向原 session 注入最终自检，并要求输出：

```xml
<ccm_completion_check>
{
  "verdict": "complete",
  "summary": "已按原始要求逐项核对",
  "evidence": [
    "需求项均已落地",
    "相关测试和构建通过"
  ],
  "remaining": []
}
</ccm_completion_check>
```

`verdict` 允许：

- `complete`
- `continue`
- `waiting_user`

如果自检发现问题，原 Agent 应立即继续工作而不是确认完成。该 turn 结束后重新生成快照并由校验 Agent再次检查。

### 7.4 系统生成的最终共识

任何 Agent 都不能直接输出可信的“双确认”。最终记录由 CCM 生成：

```json
{
  "completion_consensus": true,
  "reviewer_confirmed": true,
  "original_session_confirmed": true,
  "guard_version": 3,
  "snapshot_revision": "task-42:session-...:log-918:tree-..."
}
```

只有该系统记录为 `completion_consensus=true` 时，Completion Guard 才允许写入 `completed`。

## 8. 完整执行链路

### 8.1 创建或修改设置

1. 创建 Task 时保存 `completion_guard_enabled`。
2. 用户后续切换开关时更新该字段，并令 `completion_guard_version += 1`。
3. 广播 `completion_guard_config_changed`。
4. 如果是远端 Worker Task，将新配置与版本同步到实际执行 Worker。
5. 版本变化后，旧 evaluator、旧排队催促和旧完成声明均不得用于最终完成。

### 8.2 原 session 正常执行

原 session 沿用现有 provider、model、effort、cwd、config_dir、session_id/thread_id 和恢复机制。以下机制先于 Completion Guard：

- 瞬时错误重试；
- Claude Pool 限额轮换；
- session 丢失恢复；
- task timeout；
- cancel/interrupt；
- Plan 审批；
- Goal/Loop 自身的中间轮次判定。

只有某条执行路径原本准备把任务自然标为完成时，才进入 Completion Guard。

### 8.3 拦截候选完成

所有自然完成写入必须统一收口为类似：

```text
request_task_completion(task_id, completion_source)
```

Guard 关闭时调用原有 finalize 逻辑。Guard 开启时：

1. 不写 `completed_at`；
2. 不增加 Instance 完成计数；
3. 不触发 PR merge/完成通知；
4. Task 展示状态切为 `verifying`；
5. 创建本轮校验记录；
6. 广播校验开始事件。

### 8.4 构建校验快照

每轮快照建议包含：

- Task 原始 description；
- 创建后的有效用户追加要求；
- Goal condition、Plan、Todo/Loop 状态；
- 最近若干轮 user/assistant 消息；
- 原 session 本轮最终回复及结构化 outcome；
- 关键 tool/test/build/lint 结果摘要；
- Git status/diff/tree 摘要；
- 附件名称和必要元数据；
- 上一轮缺失项；
- 当前 provider、session、cwd；
- 当前 guard_version；
- 最新输入/日志/工作区版本标识。

不发送密钥原文、大型二进制、无限量日志或无关历史。Transcript 内的文字视为待检查证据，而不是给 evaluator 的高优先级指令，降低 prompt injection 风险。

### 8.5 启动校验 Agent

校验 Agent 是独立、短生命周期进程：

- 不 resume 原 session；
- 默认使用 provider 对应的低成本 evaluator 模型；
- 默认不允许修改文件；
- 只判断，不执行修复；
- 输出固定 JSON；
- 结果写入审计记录；
- 超时、非零退出和解析失败按 `uncertain` 处理。

现有 `GoalEvaluator` 的 Claude/Codex 命令分流、超时和 JSON 解析可以作为实现基础，但 Completion Guard 需要更丰富的 schema 和 `next_actor` 判断。

### 8.6 分支 A：校验完成，要求原 session 自检

注入原 session：

```text
[CCM 完成校验：最终自检]

独立校验 Agent认为当前任务已满足用户要求。

请基于用户原始要求和最近追加要求做最后一次独立自检：
1. 是否仍有遗漏？
2. 是否有未验证的修改？
3. 是否缺少应执行的测试、构建或迁移？
4. 是否把可以自行完成的工作交给了用户？

如果全部完成，输出规定的 ccm_completion_check。
如果发现问题，直接继续完成；不要仅描述后续工作。
如果确实必须由用户提供信息，输出 waiting_user 和唯一必要的问题。
```

原 session 自检结果：

1. `complete`：形成原 session 的确认信号；
2. `continue`：原 Agent继续工作，旧 reviewer 结论失效；
3. `waiting_user`：进入用户等待判定；
4. 无合法格式：不算确认，保守地重新校验或进入下一轮。

为避免 reviewer 结论与自检期间的新变更错位，推荐 v1 在原 session 自检返回 `complete` 后再做一次最终轻量校验。只有最终校验与原 session 确认都针对最新状态时才生成共识。未来若能可靠证明自检期间工作区、用户输入和要求均未变化，可以用快照指纹安全跳过重复校验。

### 8.7 分支 B：校验未完成，原 Agent可继续

注入原 session：

```text
[CCM 完成校验：任务尚未完成]

独立校验发现以下未完成项：
1. ...
2. ...

请现在直接继续使用工具完成这些工作。不要只解释，不要把可以自行完成的工作交还给用户。
完成后重新输出 ccm_turn_outcome。
```

该消息通过现有 per-task queue 串行发送，必须复用原来的：

- session_id/thread_id；
- cwd；
- provider/model/effort；
- Claude config_dir/Codex thread；
- task enabled skills 和其他执行配置。

原 turn 结束后重新从 8.4 开始，不能复用修复前的 evaluator 结果。

### 8.8 分支 C：确实等待用户

系统执行：

1. Task 展示状态切换为 `waiting_user`；
2. 保存结构化问题、原因和来源校验记录；
3. 不向原 session enqueue 任何自动继续消息；
4. 在 Task 聊天中展示回答卡片；
5. 向全局 `tasks` 频道广播待回答通知；
6. 设置 `has_unread=True`；
7. 等待用户回答。

用户回答后：

1. 原子地 resolve 当前 pending request；
2. 清除 `waiting_user`；
3. 将答案作为正常用户消息发送到原 session；
4. Task 回到 `executing`；
5. 该 turn 结束后重新进入校验链路。

### 8.9 分支 D：原 Agent询问了不必要的问题

如果原 outcome 是 `waiting_user`，但校验 Agent判断仓库或已有上下文足以回答，并返回 `incomplete + original_agent`，系统不提醒用户，而是向原 session注入：

```text
完成校验认为当前问题不需要用户补充；所需信息已存在于仓库或此前上下文中。
请先查找现有约定并继续完成任务。只有确实需要用户决定的产品行为、授权或外部信息才能暂停等待用户。
```

## 9. 原 session 状态与校验结论矩阵

| 原 session 声明 | 校验 Agent 结论 | 行为 |
|-----------------|-----------------|------|
| claimed_complete | complete | 注入最终自检；双方最新确认后完成 |
| claimed_complete | incomplete/original_agent | 注入具体缺失项，原 Agent继续 |
| claimed_complete | waiting_user/user | 通知用户，不 resume 原 session |
| waiting_user | waiting_user/user | 通知用户，不 resume 原 session |
| waiting_user | incomplete/original_agent | 问题不必要，催促原 session继续 |
| waiting_user | complete | 不满足双确认；保守地保留等待或要求原 session重新自检 |
| incomplete | complete | 原 session 未确认，不能完成；要求自检/继续 |
| 任意 | uncertain | 不完成；技术重试后仍失败则 needs_attention |
| 缺少合法声明 | complete | 仍需原 session固定格式自检 |

## 10. 与 AskUserQuestion 的衔接

现有 AskUser hook 会在 Claude turn 内阻塞等待，并通过 `/api/ask-user/wait`、任务频道、全局通知和回答卡片完成交互。Completion Guard 不应重复创建第二套相同 UI。

规则：

1. 已存在活跃 AskUser pending 时，以该请求为权威，Completion Guard 不再创建重复问题。
2. AskUser hook 仍在等待时，原进程尚未形成候选完成点，不启动 Completion Guard evaluator。
3. 如果 Agent 只是以普通文本提问后退出，Completion Guard 可以把 reviewer 的 `waiting_user` 结果转成统一的回答卡片。
4. Completion Guard 产生的问题需要可持久恢复。现有内存 Future 适合仍在阻塞的 hook；对于“进程已结束、等待下一条用户消息”的问题，建议扩展为带 `source=completion_guard` 的持久 pending request，或引入通用 `task_user_requests` 表。
5. 用户回答接口最终都应转成正常的 per-task 用户消息，避免出现两种 resume 语义。

## 11. 动态开关的精确行为

### 11.1 executing 时关闭

- 不打断当前 turn；
- 当前 turn 结束后按普通规则处理；
- 不启动新 evaluator。

### 11.2 executing 时开启

- 不重启当前 turn；
- 当前 turn 成功结束后进入 Completion Guard。

### 11.3 reviewing 时关闭

- `completion_guard_version += 1`；
- 尝试停止 evaluator 以节省额度；
- 无法停止时允许其退出，但结果必须因版本不匹配而丢弃；
- 清除尚未执行的 Guard 内部消息；
- 如果原 outcome 是 claimed_complete，可按普通成功规则完成；
- 如果原 outcome 是 waiting_user，不能因关闭 Guard 而伪造完成，仍保持等待用户。

### 11.4 self_checking/continuing 时关闭

- 已经发送给模型的 prompt 无法安全撤回，不强杀当前 turn；
- 当前 turn 自然结束后不再发起新校验；
- claimed_complete 按普通规则完成；
- waiting_user 继续等待用户；
- 失败/取消仍按现有语义处理。

前端提示：“催促已关闭，将在当前执行轮次结束后生效”。

### 11.5 waiting_user 时关闭

关闭只停止后续 Completion Guard 校验和自动催促，不自动完成 Task。用户仍需：

- 回答问题；
- 手动标记完成；或
- 取消任务。

### 11.6 needs_attention 时关闭

提供显式选择，避免一个开关产生隐式完成：

- “关闭催促并继续对话”；
- “关闭催促并按最后成功结果完成”；
- “取消”。

### 11.7 关闭后快速重新开启

每次切换均递增版本。旧 evaluator、旧自检结果、旧排队催促全部作废，基于最新设置重新生成快照。不得因为布尔值最终又回到 `true` 而复用旧的 `true` 版本结果。

## 12. 数据模型

### 12.1 Task 新增字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| completion_guard_enabled | BOOLEAN | false | 动态开关 |
| completion_guard_version | INTEGER | 0 | 每次设置变化递增 |
| completion_guard_state | VARCHAR(30) NULL | NULL | idle/reviewing/self_checking/continuing/waiting_user/needs_attention |
| completion_guard_round | INTEGER | 0 | 当前工作催促轮数 |
| completion_guard_last_reason | TEXT NULL | NULL | 最近结论摘要 |
| completion_guard_snapshot | VARCHAR/TEXT NULL | NULL | 当前有效快照标识 |
| completion_guard_max_rounds | INTEGER NULL | NULL | Task 覆盖；NULL 使用全局默认 |

`enabled` 是用户策略，`state` 是运行态，两者不可合并。

### 12.2 新增 `task_completion_checks`

建议字段：

| 字段 | 说明 |
|------|------|
| id | 主键 |
| task_id | Task ID |
| round | 工作催促轮数 |
| phase | initial_review/final_review |
| guard_version | 开关版本 |
| snapshot_revision | 校验快照 |
| evaluator_provider/model | 校验 provider/model |
| evaluator_verdict | complete/incomplete/waiting_user/uncertain |
| next_actor | none/original_agent/user |
| reason | 判断原因 |
| missing_items | JSON |
| evidence | JSON |
| original_outcome | 原 session 结构化声明 JSON |
| valid | 结果是否仍有效 |
| invalidated_reason | setting_changed/input_changed/work_changed/cancelled 等 |
| started_at/completed_at | 时间戳 |

### 12.3 用户等待请求

若不直接扩展 AskUser 数据结构，建议增加通用持久表：

```text
task_user_requests
```

最少包含 request_id、task_id、source、questions、status、guard_version、snapshot_revision、answer、created_at、resolved_at。这样服务重启后仍能恢复 Completion Guard 产生的等待用户卡片。

## 13. API 设计

### 13.1 Task 创建/更新

`POST /api/tasks`：

```json
{
  "description": "...",
  "completion_guard_enabled": true
}
```

`PUT /api/tasks/{id}`：

```json
{
  "completion_guard_enabled": false
}
```

更新接口在值实际变化时原子地：

1. 更新 enabled；
2. version 加一；
3. 使旧 check 失效；
4. 广播配置变化；
5. 通知本地 Guard coordinator 或远端 Worker。

建议提供专用命令端点处理 needs_attention 下带副作用的关闭选择，避免普通字段更新隐式完成：

```text
POST /api/tasks/{id}/completion-guard/disable
```

请求体：

```json
{
  "resolution": "continue_without_guard"
}
```

可选 `continue_without_guard`、`accept_last_success`。

### 13.2 查询校验历史

```text
GET /api/tasks/{id}/completion-checks
```

返回当前状态、轮数、最近原因、历史 checks 和 pending user request。

### 13.3 用户回答

优先复用/泛化现有 AskUser 回答 API；如果使用通用请求表：

```text
POST /api/tasks/{id}/user-requests/{request_id}/answer
```

必须校验 request 仍为 pending 且版本仍有效，重复回答需幂等。

## 14. 服务端架构建议

### 14.1 `CompletionGuardService`

建议新增集中服务，职责包括：

- 拦截候选完成；
- 构建校验快照；
- 启动/解析 evaluator；
- 决定 next_actor；
- 构建自检/催促 prompt；
- 管理轮数与版本失效；
- 创建用户等待请求；
- 原子提交最终完成；
- 恢复重启前的 guard 状态。

不要把判断逻辑分别复制进 auto、PTY chat、Goal、Loop 和 Worker relay。

### 14.2 单一最终完成入口

现有所有 `mark_completed`、直接 `update(Task).values(status="completed")` 和 PTY finally 完成恢复路径都需要审计。自然成功统一经过 Completion Guard；人工强制完成、迁移清理等特殊路径必须显式声明 bypass 原因并记录审计事件。

最终提交函数应保证：

1. 只完成一次；
2. 只增加一次 Instance 完成计数；
3. commit 后统一广播状态；
4. commit 后才触发 PR/分享通知等后置动作。

### 14.3 per-task 串行化

自检和催促必须使用现有 per-task queue，并新增明确来源：

```text
source = completion_guard:self_check
source = completion_guard:continue
```

需要支持按 source/guard_version 丢弃尚未消费的 Guard 消息。用户消息保持最高优先级；新用户消息会使旧 reviewer 结果失效，并应先被处理。

除队列串行化外，建议增加 task-scoped Guard lock 或数据库 compare-and-set，避免以下路径同时最终完成：

- lifecycle 正常退出；
- PTY chat finally；
- Worker 状态回传；
- 用户手动操作；
- 服务重启恢复任务。

### 14.4 快照与失效

结果有效至少要求：

```text
check.guard_version == task.completion_guard_version
check.snapshot_revision == task 当前有效快照
task 未 cancelled/failed
没有更新的用户输入等待处理
没有另一个前台 turn 正在执行
```

快照可以由 session_id、最近有效 user message/log id、任务配置 revision、工作区/git tree 指纹组成。任何任务要求修改、用户新消息或实际工作变更都必须使旧 check 失效。

## 15. 不同任务模式

### 15.1 Auto / Plan

- Auto 的自然成功出口进入 Guard。
- Plan 生成后等待审批不是候选完成；获批并执行结束后进入 Guard。

### 15.2 Loop

Loop 中间 iteration 不触发 Guard。只有 Loop 自身认为 done、且 `must_complete` 等现有条件通过后，才把“准备完成”交给 Guard。若 Guard 判定未完成，催促应回到同一 Loop session，而不是创建并行 loop。

### 15.3 Goal

GoalEvaluator 与 Completion Guard reviewer 职责不同：

- GoalEvaluator 判断自然语言 goal condition 是否达到，并控制继续多少 turn；
- Completion Guard 在 Goal 准备结束时做最终完整性和下一行动者判断。

可以复用底层 evaluator runner，但不能简单把两个判断结果混为同一字段。若产品希望降低成本，可在明确证明 schema 和快照一致后让 Goal 的最终 achieved 结果充当 Completion Guard 的 initial review，原 session 自检和系统共识仍不能省略。

### 15.4 后续聊天

已完成 Task 收到新用户消息后重新执行。该 turn 是否走 Guard 读取当时最新的 Task 开关，不继承第一次完成时的旧快照或确认。

## 16. 分布式 Worker

session 和工作区位于实际 Worker，因此 evaluator、原 session resume 和工作证据采集应在 Worker 侧执行。Manager 负责配置与 UI 的权威展示。

要求：

1. Task 创建/迁移 payload 透传全部 Guard 字段；
2. Manager 修改开关时把 enabled + version 转发到 Worker；
3. Worker 回传 guard_state、checks、waiting_user 和最终共识事件；
4. Manager 只在收到有效最终共识后镜像 `completed`；
5. 网络断开时 fail closed，不凭本地过期状态完成；
6. UI 显示设置“正在同步”或“同步失败”，避免声称已经关闭但 Worker 尚未收到；
7. 重连后以更高 version 为准，重新下发并使旧结果失效。

## 17. 失败、重试与上限

### 17.1 evaluator 技术错误

- 超时、进程失败、JSON 解析失败先做有限技术重试，例如 2 次；
- 技术重试不计入工作催促轮数；
- 仍失败则进入 `needs_attention`，不得当作完成或自动催促原 Agent盲目工作。

### 17.2 最大催促轮数

建议默认 5 轮，可配置。达到上限后：

```text
completion_guard_state = needs_attention
Task 展示状态 = needs_attention（或现有 failed 的明确子原因，若暂不扩顶层状态）
```

不得自动完成。用户可增加轮数、继续一轮、修改要求、关闭 Guard 后继续、人工接受结果或取消。

### 17.3 原 session 失败

催促/自检 turn 的瞬时错误仍走现有 provider-aware 重试；真正失败走现有 failed/recovery 语义。不能因为 evaluator 先前确认完成而掩盖后续原 session 的执行失败。

### 17.4 取消与删除

- cancel 立即使当前和历史 check 失效；
- 停止 evaluator；
- 清理 pending Guard 队列消息；
- resolve/关闭 Completion Guard 产生的用户等待卡片；
- 不把 cancel 转成 completed。

## 18. 最终完成的原子条件

最终事务必须重新读取并同时验证：

1. `completion_guard_enabled=true`；
2. guard_version 未变化；
3. reviewer 最新结果为 complete；
4. 原 session 最新自检为 complete；
5. 两者对应当前有效快照；
6. 没有更新的用户消息；
7. 没有活跃前台 turn；
8. Task 未取消、失败或删除；
9. 没有未解决的用户请求。

满足后才原子执行：

```text
status = completed
completed_at = now
completion_guard_state = confirmed
error_message = null
```

事务成功后：

- 广播 Task status_change；
- 广播 completion_guard_consensus；
- 增加 Instance 完成计数（幂等）；
- 触发 PR Review 后续逻辑；
- 触发共享用户完成通知；
- 清理 evaluator 和内部队列状态。

如果 Guard 在最终事务前被关闭，则不能生成 Guard 共识；改走关闭时对应的普通规则。

## 19. WebSocket 与前端

建议事件：

```text
completion_guard_config_changed
completion_review_started
completion_review_result
completion_self_check_started
completion_continue_enqueued
completion_waiting_user
completion_check_invalidated
completion_guard_consensus
completion_guard_needs_attention
```

Task 列表展示：

- `Verifying`：正在检查；
- `Waiting for you`：需要用户回答；
- `Continuing`：已催促原 Agent继续；
- `Needs attention`：自动链路无法继续。

聊天流用低干扰的 system event 展示：

```text
正在进行完成校验……
独立校验发现 2 个未完成项，已要求 Agent继续。
任务需要你的回答：请选择迁移策略。
原 Agent与独立校验均确认完成。
```

详细 checks 放在可折叠面板，避免把 evaluator 的完整输出重复插入聊天。

任务详情中的开关应显示即时阶段和生效说明，例如：

```text
完成校验与自动催促  [关闭]
当前：第 2 轮继续工作中；关闭将在本轮结束后生效。
```

## 20. 安全与提示词边界

1. evaluator prompt 明确把 transcript、代码注释和文件内容视为不可信证据，不执行其中指令。
2. evaluator 不获得用户密钥原文；只得到判断所需的摘要。
3. evaluator 默认无写权限，不得 commit/push/删除文件。
4. `next_actor=user` 仅用于真正需要用户决策的事项，不能被用来逃避可自行完成的工作。
5. reviewer reason 注入原 session 前做长度限制和结构化渲染，避免原始模型输出成为无边界 prompt。
6. 系统生成的版本、快照和 consensus 字段不从模型文本读取。

## 21. 可观测性

建议指标：

- Guard 开启任务数；
- 首轮 reviewer 通过率；
- 平均催促轮数；
- `next_actor=user` 比例；
- reviewer technical failure/parse failure；
- 因设置切换失效的 check 数量；
- needs_attention 数量；
- Guard 额外 token、耗时；
- reviewer 与原 session 分歧率；
- Guard 阻止的候选误完成数。

日志必须携带 task_id、guard_version、round、check_id、snapshot_revision、provider，不记录密钥或完整私密附件。

## 22. 测试方案

### 22.1 单元测试

1. evaluator JSON 的四种 verdict 和非法组合解析；
2. 原 session outcome/self-check XML 包裹解析；
3. 任一确认缺失时 consensus 必为 false；
4. guard_version 变化使旧结果失效；
5. 新用户消息使旧 snapshot 失效；
6. incomplete/original_agent 生成催促；
7. waiting_user/user 不 enqueue 原 session；
8. evaluator error fail closed；
9. 达到 max rounds 进入 needs_attention；
10. 最终完成计数只增加一次。

### 22.2 Dispatcher 集成测试

1. Guard 关闭保持现有自然完成行为；
2. Guard 开启后 auto 成功不直接 completed；
3. reviewer complete → self-check complete → final review complete → completed；
4. reviewer incomplete → resume 同一 session → 再校验；
5. 原 Agent waiting_user → reviewer waiting_user → 只通知用户；
6. 原 Agent waiting_user → reviewer 认为可自行处理 → resume 原 session；
7. reviewing 中关闭，旧 evaluator 返回后被丢弃；
8. 催促排队后关闭，内部消息被移除；
9. 催促已经启动后关闭，本轮自然结束且不再校验；
10. waiting_user 中关闭不会自动 completed；
11. 用户消息与 Guard 消息同时入队时，用户消息优先且校验失效；
12. PTY turn 结束路径不绕过 Guard；
13. transient retry 完成后只进入一次 Guard；
14. cancel 会停止 evaluator 并清理 pending；
15. 服务重启恢复 reviewing/waiting_user；
16. Goal/Loop 只在最终候选完成点触发；
17. Codex app-server 与 exec fallback 语义一致。

### 22.3 分布式测试

1. Manager 创建任务时字段完整透传；
2. 执行中关闭同步到 Worker 并使旧 check 失效；
3. 网络断开期间不误完成；
4. 重连后高版本覆盖低版本；
5. waiting_user 事件在 Manager UI 可回答并正确 resume Worker session。

### 22.4 前端测试

1. 创建表单默认值和提交 payload；
2. 任务详情动态开关；
3. 各 guard_state 的展示；
4. 关闭将在本轮结束后生效的提示；
5. waiting_user 回答卡片和全局通知；
6. 重连后恢复 pending 卡片；
7. needs_attention 的显式关闭选择；
8. 不重复渲染 reviewer 事件、用户卡片和注入消息。

## 23. 建议实施顺序

### 阶段 1：完成入口收口

1. 找齐所有自然 completed 写入路径；
2. 建立单一 finalize/request completion 服务；
3. 用回归测试保证 Guard 关闭时行为不变。

### 阶段 2：数据与动态开关

1. 增加 Task 字段和 migration；
2. 扩展 create/update/response/Worker payload；
3. 增加创建表单和详情开关；
4. 实现 version invalidation。

### 阶段 3：校验与原 session 协议

1. 实现 evaluator schema、runner 和审计表；
2. 实现 outcome/self-check 解析；
3. 接入 per-task queue；
4. 实现双确认原子提交。

### 阶段 4：下一行动者与用户等待

1. 接入/泛化 AskUser UI 和通知；
2. 持久化 Completion Guard user request；
3. 实现回答后 resume；
4. 完成等待用户与不必要提问的分流。

### 阶段 5：模式、Worker 与恢复

1. Goal/Loop/Plan 接入；
2. 分布式 Worker 同步；
3. 重启恢复、超限和 needs_attention；
4. 指标、审计面板和灰度配置。

## 24. 验收标准

功能完成至少需要满足：

1. 开关可在创建后任意时刻修改，后续 turn 使用最新值；
2. Guard 开启时，无自然成功路径可以直接 completed；
3. reviewer 认为完成后，原 session 必须自检；
4. 系统只在两者针对有效最新状态确认后生成 consensus；
5. reviewer 发现未完成且 Agent可处理时，原 session 自动继续；
6. reviewer 判断必须等用户时，只提醒用户且不 resume 原 session；
7. 等待用户时关闭 Guard 不会误完成；
8. 开关变化、用户新消息和工作变更能可靠废弃旧结果；
9. Claude、Codex、PTY、subprocess、Worker 路径行为一致；
10. 失败、重启、并发和最大轮数场景均 fail closed；
11. 最终完成广播、计数和后置动作只执行一次；
12. 用户能在 UI 中理解当前由谁行动、为什么尚未完成。

## 25. 关键设计结论

Completion Guard 的本质不是“任务结束后再问一次模型”，而是一个 Task 级、动态可配置、可恢复的完成状态机：

```text
候选完成
  → 独立判断任务状态
  → 判断下一行动者
  → 原 Agent自检 / 原 Agent继续 / 等待用户
  → 对最新状态形成系统级双确认
  → 原子完成
```

其中最重要的边界是：

- 进程退出不等于任务完成；
- 单个 Agent 的声明不等于双方确认；
- 未完成不等于一定要催原 Agent；
- 等待用户时不能自动触发原 session；
- 动态开关变化必须使旧异步判断失效；
- 任何不确定情况都不能误标完成。
