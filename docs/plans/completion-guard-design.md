# 任务完成校验与自动催促设计

> 状态：设计稿，尚未实施。2026-07-20 已按当日 main（d7b2402）逐项代码核查并修订：所有「现状/落地校对」段落与附录 A 均为一手证据（文件:行号）。
>
> 本文定义 Task 级“完成校验与自动催促”（下文简称 Completion Guard）的产品语义、状态机、消息协议、并发规则和建议改动。它适用于 Claude 与 Codex provider，并与现有 per-task 消息队列、AskUserQuestion、Goal/Loop、分布式 Worker 和任务状态广播机制衔接。
>
> **只需要看 §0（总览）+ §0.1（决策清单）+ §0.2（To-do 总表）**即可完成 check；§1 起的正文和附录是实施时的参考细节。

## 0. 一页纸总览：这个方案在干嘛

CCM 今天判定「任务完成」的标准本质上是**进程正常退出**：Claude/Codex 的一轮 turn 以 exit 0 结束，dispatcher 就把 Task 写成 completed。这个标准太弱——agent 只干了一半就总结收工、漏跑测试和迁移、把自己能干的活「交还给用户」、或者声称完成但你刚追加的要求根本没做，这些今天都会被标成 completed，只能靠你自己翻聊天记录发现。本方案（Completion Guard）把「进程退出」和「任务真正完成」拆开：开启后，一轮正常退出只算**候选完成点**，系统先派一个独立、便宜的校验 Agent（复用现有 GoalEvaluator 链路，claude 用 haiku / codex 用 gpt-5.4-mini）读任务要求、对话和工作区证据做判断。结果分三路：**① 它认为完成了**——还不算数，要让原 session 自己再做一次最终自检，双方都确认、且期间没有新变化，由 CCM 系统（而不是任何一个模型）生成「双确认共识」记录，这时才真正写 completed；**② 没完成、但缺的是 agent 自己能干的活**（补测试、跑构建、查仓库里已有的约定）——系统自动把具体缺失项注进原 session 催它继续干，全程不打扰你；**③ 没完成、且下一步必须你来**（产品方案二选一、不可逆操作确认、给凭证）——只弹卡片通知你，绝不自动唤醒原 session。催促最多 5 轮，超限或校验器自身故障（超时/解析失败）一律 fail closed：进 needs_attention 等你显式处理，宁可挂着也不误标完成。你主动 Ctrl-C、手动 stop 这类操作不进这套流程，按现有语义直接完成。

工程上最大的一块前置工作是「收口」：今天全仓有 14 处代码在写 completed（已逐处盘点在附录 A），行为还互相不一致（有的不写完成时间、不计数），必须先收成一个统一入口，Guard 才有唯一的拦截点；这一步对 Guard 关闭的任务行为完全不变，可以独立先做。开关是 Task 级、随时可切的动态设置，每次切换递增版本号，所有异步判断结果都绑版本、过期作废——这是防「切换期间旧判断误生效」的关键机制。分布式 worker 任务的校验在 worker 侧跑，Manager 需要新建一条配置下发通道（目前不存在）。

你需要拍板的事，按重要性：**第一**，②/③ 的分流标准你认不认——什么算「agent 该自己干」vs「必须问你」（§5 有例子清单）；**第二**，聊天场景的成本折衷——每条聊天消息的 turn 结束今天都会写一次 completed（空闲语义），Guard 开着就意味着每条消息都要过校验器，默认方案是短路（只在 agent 明确声称完成、或任务从未达成过共识时才真校验），代价是共识之后被 agent 低估的追加要求可能漏拦；**第三**，5 轮上限与 needs_attention 的处理方式，以及任务列表会多出 Verifying / Waiting for you 等新状态这些产品外观。逐条决策清单见 §0.1，完整实施 to-do（含测试）见 §0.2。

## 0.1 需要你确认的核心决策

一句话概括：**进程正常退出不再等于任务完成**。开启后，每个「候选完成点」要经过独立校验 Agent 判定 + 原 session 自检 + 系统生成共识，三者齐备才写 `completed`；未完成时系统判断该催 Agent 还是该问你。

需要你逐条 check 的产品决策（括号内是详细章节）：

1. **双确认才算完成**：校验 Agent 和原 session 单方声明都不算数，共识记录由 CCM 系统生成；任何异常（超时/解析失败/版本过期/达到上限）一律 fail closed，宁可不完成也不误标完成（§7.4、§18）。
2. **未完成时由谁行动**：校验 Agent 必须判 `next_actor`——缺测试/没构建这类 Agent 自己能干的，自动注入催促继续干（不打扰你）；产品方案选择/不可逆操作/凭证这类必须你决定的，只通知你、绝不自动 resume 原 session。分流标准的例子在 §5.2/§5.3，请确认边界符合预期。
3. **不进 Guard 的旁路**：你主动中断（Ctrl-C）、手动 stop-session、PR review 任务被新 push 取代、迁移回滚——按现有语义直接处理，不触发校验（附录 A 的 bypass 分类）。
4. **聊天成本折衷（需拍板）**：Guard 开着时每条聊天消息的 turn 结束都是候选完成点。默认方案是廉价短路：只在 Agent 声明完成、或任务从未达成过共识时才跑重校验——代价是共识后被 Agent 低估的追加要求可能漏拦（§15.4 有残余风险声明）。不接受可选每 turn 全量校验（更贵）。
5. **催促上限与兜底**：默认最多催 5 轮（可配置），超限进 `needs_attention` 等你显式处理（继续/加轮数/关 Guard/手动完成/取消），永不自动完成（§17.2）。
6. **UI 变化**：任务列表新增 `Verifying` / `Waiting for you` / `Continuing` / `Needs attention` 展示态；聊天流用低干扰 system event 提示，详细校验记录折叠在面板里（§6、§19）。
7. **成本**：每轮校验一次低成本模型调用（claude 用 haiku、codex 用 gpt-5.4-mini，复用 GoalEvaluator 链路）；每轮催促消耗原 session 正常 token（§8.5、§21）。
8. **开关动态语义**：任务创建后任意时刻可开关；执行中关闭在本轮结束后生效；`waiting_user` 时关闭只停催促、不自动完成——你仍需回答/手动完成/取消（§11）。
9. **Worker 任务的工程量**：Guard 在 worker 侧执行；Manager 改开关要同步到 worker，但该配置下发通道目前不存在需要新建，且是唯一一处 Manager→Worker 的配置同步（§16）。

## 0.2 实施 To-do 总表（含测试）

按依赖顺序分 5 个阶段，每项括号内为详细章节。测试项就地列在对应阶段，不后置。

**阶段 1：完成入口收口**（可独立先行，Guard 关闭时行为不变）

- [ ] 按附录 A 盘点表重新校验全部 completed 写入路径（代码演进后先 grep 复核）
- [ ] 建单一 finalize 服务 `request_task_completion`，7 处自然成功路径全部改走它（§8.3、§14.2）
- [ ] 顺带修既有不一致：completed_at 缺失 / Instance 完成计数 / PR review 回查只覆盖 auto 路径（§14.2）
- [ ] 重启 stale 兜底 `_cleanup_stale_state` 与 finalize 的关系理顺（§14.2 冲突项）
- [ ] 测试：Guard 关闭时各路径行为逐条锁定为回归基线（§22.2-1；「现有行为」按路径不一致，需逐条定义预期）

**阶段 2：数据与动态开关**

- [ ] Task 新增 Guard 字段 + alembic migration（§12.1）
- [ ] 新增 `task_completion_checks` 审计表（§12.2）
- [ ] create/update API：值变化时原子递增 version、失效旧 check、广播（§13.1）
- [ ] worker 三处 payload 透传 Guard 字段，并补齐迁移 payload 既有丢字段（§16）
- [ ] 前端：创建表单开关 + 任务详情动态开关（§19）
- [ ] 测试：version 变化使旧结果失效（§22.1-4）；payload 字段覆盖度断言（§22.3-1）；前端表单默认值/提交（§22.4-1,2）

**阶段 3：校验与原 session 协议**

- [ ] evaluator schema + runner（复用 GoalEvaluator，技术失败显式落 `uncertain`）（§7.2、§8.5）
- [ ] outcome / self-check 标签解析（§7.1、§7.3）
- [ ] Guard 消息接入 per-task queue：新增 `PRIORITY_GUARD` 档位 + 消费侧版本校验丢弃（§14.3）
- [ ] 双确认原子提交 + 共识广播 + 后置动作只执行一次（§7.4、§18）
- [ ] 测试：四种 verdict 与非法组合解析（§22.1-1,2）；缺任一确认 consensus=false（§22.1-3）；催促生成 / evaluator error fail closed / 完成计数只加一次（§22.1-6,8,10）；auto 成功不直接 completed → 全链路 completed（§22.2-2,3,4）；reviewing 中关闭丢弃旧结果 / 排队催促被清（§22.2-7,8,9）；PTY turn 不绕过 Guard / transient retry 只进一次 Guard（§22.2-12,13）；Codex app-server 与 exec fallback 一致（§22.2-17）

**阶段 4：next_actor 与用户等待**

- [ ] `task_user_requests` 持久表 + 回答 API（重启可恢复；对齐现有 AskUser 端点风格）（§12.3、§13.3）
- [ ] waiting_user 通知复用 AskUser 卡片/全局通知/重连回填链路（§10）
- [ ] 回答后转正常用户消息 resume + 「不必要提问」分流催促（§8.8、§8.9）
- [ ] 测试：waiting_user 不 enqueue 原 session（§22.1-7）；两种 waiting_user 分流（§22.2-5,6）；waiting_user 中关闭不自动完成（§22.2-10）；用户消息优先且使校验失效（§22.2-11）；前端回答卡片/全局通知/重连恢复（§22.4-5,6）

**阶段 5：模式、Worker 与恢复**

- [ ] Goal/Loop/Plan 介入点接入（§15；测试 §22.2-16）
- [ ] Worker：配置下发通道（新建）+ relay 对 completed 的共识守卫 + guard 事件回传（§16）
- [ ] 重启恢复 reviewing/waiting_user，stale 兜底不洗白 Guard 任务（§14.2；测试 §22.2-15,18,19）
- [ ] cancel 清理 evaluator/队列/用户卡片（§17.4；测试 §22.2-14）
- [ ] needs_attention 显式关闭选择端点（§11.6、§13.1；前端 §22.4-7）
- [ ] 分布式测试全组（§22.3）；指标与审计面板（§21）

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

现状核查（2026-07-20）：`Task.status` 是无约束字符串（`String(20)`，`models/task.py:15-17`，无 Enum/CHECK；前端 `client.ts` 的 Task 接口也是 `status: string`，并无集中式 TaskStatus union），新增展示状态没有 schema 成本。但必须过一遍所有按状态字面量分支的消费方并逐个决定如何对待新状态：任务列表徽标/过滤、ChatView 的 localStatus 覆盖逻辑、monitor/sub-agent 创建 API 的活跃状态白名单（`api/monitor.py` 只认 `in_progress/executing`）、`queue.cancel`、worker relay 状态镜像等。新状态的每次写入同样受「状态变更必广播」约定约束。

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

注入位置：auto/goal/loop 的 prompt 由 dispatcher 构建（`_agent_doc_preamble` 同款前导模式），可直接追加协议说明；chat 后续 turn 的 prompt 是用户消息原文，协议说明只能依赖原 session 的上下文记忆，必要时由系统低频补挂（如开关刚打开后、或快照失效后的第一条消息尾部），不要每条消息重复注入污染对话。这也是「声明缺失不得默认完成」必须成立的原因——chat 场景下声明缺失是常态而非异常。

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

落地校对（2026-07-20 代码核查，完整路径盘点见附录 A）：

- 真正需要经过 Guard 的「自然成功」写入共 7 处：dispatcher 的 `_run_task_lifecycle` exit0（`dispatcher.py:1341-1363`，唯一挂 PR review 回查的路径）、`_run_transient_retry` / `_run_pool_retry` 恢复成功、`_run_loop_iterations` done、`_run_goal_lifecycle` achieved，以及 chat turn 的两处——instance_manager `_consume_output` 的 chat_initiated 分支（`instance_manager.py:868-885`，带 `chat_active_statuses` SQL 条件守卫）和 PTY 轮询兜底 `_process_queued_message`（`dispatcher.py:3259-3272`，`WHERE status=="executing"` 守卫）。其余 completed 写入（用户中断、重启 stale 兜底、stop-session、PR superseded、worker 镜像、迁移回滚）是旁路语义，按附录 A 的分类处置。
- 「不触发完成通知」有一个隐蔽通道必须封住：分享用户的飞书通知不挂在写库处，而是挂在**广播层**——`ws_broadcaster.py:70-77` 对 tasks 频道 `status_change` 且 `new_status in ("completed","failed","cancelled")` fire-and-forget 触发 `share_notifier`。因此 verifying 阶段必须以自己的 `new_status`（如 `verifying`）广播，绝不能借用 `completed`，否则外发通知会先于共识发出且无法撤回。

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

现有 `GoalEvaluator` 可以直接作为 runner 基础（2026-07-20 核查）：`evaluate()` 已接受 `provider` 参数并分流命令（`goal_evaluator.py:28-93`）——codex 走 `codex exec --json --ephemeral`、claude 走 `claude -p --output-format json --max-turns 1`；默认模型 `settings.default_goal_evaluator_model`（claude-haiku-4-5）/ `default_codex_goal_evaluator_model`（gpt-5.4-mini）；超时 `goal_evaluation_timeout`（默认 120s）。Guard 需要在其上扩展：

- schema 从 `{achieved, reason}` 扩为 `{verdict, next_actor, missing_items, user_question, evidence}`；
- 语义修正：GoalEvaluator 把超时/解析失败静默当作「未达成」继续跑，Guard 必须把这类技术失败显式落为 `uncertain`（进 §17.1 的技术重试与 needs_attention），不能与模型真实判断的 incomplete 混同；
- evaluator 的 provider 沿用现有约定复用 `task.provider`（现无独立 evaluator provider 字段，不必新增）。

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
4. Completion Guard 产生的问题需要可持久恢复。核查确认现有 registry 是纯进程内 `dict + asyncio.Future`（`services/ask_user.py:23-34`），**没有任何落库**（仅 LogEntry 审计），重启即丢——对仍在阻塞的 hook 这是合理设计（进程都没了，Future 无意义），但对 Guard 的「进程已结束、等待下一条用户消息」场景，持久化不是可选优化而是硬前提。故必须引入带 `source=completion_guard` 的持久 pending request（通用 `task_user_requests` 表，见 §12.3）。
5. 用户回答接口最终都应转成正常的 per-task 用户消息，避免出现两种 resume 语义。回答端点风格对齐现有 `POST /api/tasks/{task_id}/ask-user/{request_id}`（`api/ask_user.py:168`）；全局 pending 拉取对齐 `GET /api/ask-user/pending`（`list_all()`），前端 `AskUserNotifications` 已有的通知/回填链路可直接承载 Guard 问题。

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

completed 写入路径已全量盘点（附录 A）。自然成功统一经过 Completion Guard；人工强制完成、迁移清理等特殊路径必须显式声明 bypass 原因并记录审计事件。收口时的既有事实与陷阱：

- 中心助手 `TaskQueue.mark_completed()`（`task_queue.py:239-245`）只写 `completed_at` + 清 `error_message`——广播和 `Instance.total_tasks_completed` 计数散落在各调用方，且只有 5 处 dispatcher 成功路径计数、只有 `_run_task_lifecycle` 一处挂了 `_handle_pr_review_completion`。中断、stale 兜底、stop-session、PR superseded 等路径既不写 `completed_at` 也不计数。收口成单一 finalize 服务时应一并修掉这些不一致（阶段 1 的隐藏收益，也是回归测试的既有基线陷阱：现状本身就不一致，「保持现有行为」要按路径逐条定义）。
- chat 模式的状态判定不在 dispatcher，而在 instance_manager `_consume_output` 的 chat_initiated 分支（`instance_manager.py:868-885`）；PTY 另有 `_process_queued_message` 轮询兜底（`dispatcher.py:3259`）。两处分属不同服务、目前各靠 SQL 条件更新（`chat_active_statuses` / `WHERE status=="executing"`）防并发，改为调用统一 finalize 后这层 compare-and-set 语义不能丢。
- 所谓「PTY finally 完成恢复路径」经核查并不存在（`_launch_pty` 的 finally 只恢复 monkey-patch，不写状态）；真正相关的是 `_process_event` 的 completed→executing 复活块（`instance_manager.py:1547-1558`）。Guard 把任务停在 `verifying`（而非 completed）时复活块不会触发，但该块需要认识新增状态：自检/催促 turn 的 assistant 事件不得把 `verifying`/`waiting_user` 意外翻成 executing 或其他状态。
- 需要显式声明 bypass 的既有路径：用户中断（exit -2/130 视为完成，`dispatcher.py:1234-1248`——用户主动行为，不进 Guard，按现语义直接完成）、`stop_session` 手动完成（`api/tasks.py:389`）、PR review 任务被新 push 取代（`api/pr_monitor.py:315`）、迁移回滚恢复原状态（`task_migrator.py:124`，非完成语义）。
- **重启 stale 兜底与 Guard 直接冲突**：`_cleanup_stale_state`（`dispatcher.py:355-380`）会把重启时卡在 executing/in_progress 的任务一律标 completed。Guard 任务的重启恢复必须先于该兜底执行并把 verifying/waiting_user 任务接管走，否则一次服务重启就能把守门中的任务一键洗白成 completed，整个机制形同虚设。
- worker 镜像路径（`worker_relay.py:303-321` 无条件 `t.status = new_status`）按 §16 处理：Guard 在 worker 侧执行，relay 侧对 completed 加共识守卫是纵深防御，同时 relay 需照常镜像新增的 verifying/waiting_user 状态。

最终提交函数应保证：

1. 只完成一次；
2. 只增加一次 Instance 完成计数（字段为 `Instance.total_tasks_completed`）；
3. commit 后统一广播状态；
4. commit 后才触发 PR/分享通知等后置动作（分享通知由广播层的 status_change 钩子触发，见 §8.3 落地校对）。

### 14.3 per-task 串行化

自检和催促必须使用现有 per-task queue，并新增明确来源：

```text
source = completion_guard:self_check
source = completion_guard:continue
```

现状（2026-07-20 核查）：队列已经是 `asyncio.PriorityQueue`，元素 `QueuedMessage` 自带 `priority` 与 `source` 字段（`dispatcher.py:77-108`；`PRIORITY_USER=0` / `MONITOR_COMPLETE=1` / `MONITOR_IMPORTANT=2`，小者先出、同级按时间 FIFO）——「用户消息最高优先级」已经成立，Guard 消息只需新增更低档位（如 `PRIORITY_GUARD=3`）。

真正的缺口是选择性丢弃：现成的 `clear_task_queue`（`dispatcher.py:2830`）只能全量排空（interrupt/cancel 场景适用）。不建议实现「从队列里挑着删」（与 consumer 的并发 get 有竞态）；改为**消费侧校验**：Guard 消息携带 guard_version + snapshot_revision 入队，consumer 出队时对照 Task 当前版本，不匹配直接丢弃并记审计事件。新用户消息入队时使快照失效，即自然废弃所有在途 Guard 消息，无需触碰队列结构。

除队列串行化外，建议增加 task-scoped Guard lock 或数据库 compare-and-set，避免以下路径同时最终完成：

- lifecycle 正常退出；
- chat 完成写入（`_consume_output` chat 分支与 PTY 轮询兜底，两处分属 instance_manager 与 dispatcher）；
- Worker 状态回传；
- 用户手动操作；
- 服务重启恢复任务（含 `_cleanup_stale_state` 兜底，见 14.2）。

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

落地位置：loop 完成信号是 Claude 写的 `loop_signal_{task.id}.json`（action=done/continue/abort，`dispatcher.py:1586` / `_read_loop_signal:2175`），`must_complete` 的拒绝早退（分母锚定 `anchored_total`）在 `dispatcher.py:1736-1747`。Guard 的介入点在 action==done 且 must_complete 检查通过之后、`mark_completed`（`dispatcher.py:1753-1770`）之前。

### 15.3 Goal

GoalEvaluator 与 Completion Guard reviewer 职责不同：

- GoalEvaluator 判断自然语言 goal condition 是否达到，并控制继续多少 turn；
- Completion Guard 在 Goal 准备结束时做最终完整性和下一行动者判断。

可以复用底层 evaluator runner，但不能简单把两个判断结果混为同一字段。若产品希望降低成本，可在明确证明 schema 和快照一致后让 Goal 的最终 achieved 结果充当 Completion Guard 的 initial review，原 session 自检和系统共识仍不能省略。

落地位置：goal 成功路径在 `_run_goal_lifecycle`（`dispatcher.py:1941-1958`），它不看 exit_code、也没挂 PR review 回查；evaluator 复用 `task.provider` 与 `task.goal_evaluator_model`（`dispatcher.py:1904-1909`）。Guard 介入点在 `eval_result.achieved` 为真之后、`mark_completed` 之前。

### 15.4 后续聊天

已完成 Task 收到新用户消息后重新执行。该 turn 是否走 Guard 读取当时最新的 Task 开关，不继承第一次完成时的旧快照或确认。

成本注意：现实现里 chat 任务的 `completed` 同时承担「对话空闲」语义——每条聊天消息的 turn 结束都会再写一次 completed（`_consume_output` chat 分支）。Guard 开启时这意味着**每条聊天消息都触发一轮 evaluator**，对「谢谢」「继续」这类寒暄/短指令是纯浪费。建议 v1 提供廉价前置短路：仅当本 turn outcome 声明 `claimed_complete`、或该任务在当前要求集合下从未达成过共识时，才启动重型校验；短路跳过的 turn 沿用现有 completed 语义（该语义在 Guard 之前已存在，跳过校验≠伪造共识，consensus 记录仍只在真校验后生成）。短路规则本身要可观测（审计记录 skipped_reason）。残余风险要明示：共识达成后的追加要求若被原 Agent 低估（既不声明 claimed_complete 也不报 incomplete 就结束 turn），短路会让该 turn 退回现状的空闲 completed 语义——这是刻意的成本折衷，不是守门失效；若产品不接受，可提供「聊天 turn 全量校验」的更贵档位。

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

现状缺口（2026-07-20 核查，全部需要新建）：

- 第 2 条的配置下发通道目前**不存在**：Manager 侧 `PUT /api/tasks/{id}` 除 worker_id 迁移外只写本地 DB（`api/tasks.py:307`），任何普通字段修改都不会同步到 worker。需新建转发（可复用通用代理 `worker_proxy.proxy_to_worker`，`tasks.py:347`）。
- 新增 Guard 字段必须同时透传三处，缺一处 worker 侧就丢配置：① 首次转发 payload `forward_task_to_worker`（`worker_proxy.py:163-186`）；② 迁移重建 payload（`task_migrator.py:315-332`）；③ 迁移目标已存在时的 PUT（`task_migrator.py:308-313`，目前只传 project_id）。核查同时发现迁移 payload 本就漏了 `priority/max_retries/max_iterations/must_complete/goal_max_turns/goal_evaluator_model/thinking_budget/timeout_hours/tags` 一批字段（既有缺陷），阶段 2 动 payload 时应一并补齐并加字段覆盖度测试。
- 第 4 条的镜像守卫目前不存在：relay 收到 status_change 是无条件 `t.status = new_status`（`worker_relay.py:303-321`）。需在此处校验「completed 须伴随（或此前已收到）有效 completion_guard_consensus 事件」，作为 worker 侧 Guard 的纵深防御。

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
- 清理 pending Guard 队列消息（现成的全量 `clear_task_queue` 在 cancel 场景语义正确，可直接用）；
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
17. Codex app-server 与 exec fallback 语义一致；
18. 重启 stale 兜底（`_cleanup_stale_state`）不把 verifying/waiting_user 任务洗白成 completed；
19. `_process_event` 复活块不把 verifying/waiting_user 翻成其他状态。

### 22.3 分布式测试

1. Manager 创建任务时字段完整透传（对首次转发 payload 与迁移 payload 各做一次字段覆盖度断言，防再次出现迁移丢字段）；
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

1. 以附录 A 的盘点为基线核对所有 completed 写入路径（代码演进后重新 grep 校验一遍再动手）；
2. 建立单一 finalize/request completion 服务，顺带修掉既有不一致（completed_at / 完成计数 / PR review 回查只覆盖部分路径，见 14.2）；
3. 用回归测试保证 Guard 关闭时行为不变（注意「现有行为」本身按路径不一致，需逐条定义预期）。

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

## 附录 A：completed 写入路径盘点（2026-07-20 代码核查）

grep 全 backend/（排除 tests）所得，行号对应当日 main（d7b2402）。实施阶段 1 前应重新校验一遍。

| # | 位置 | 场景 | 广播 | completed_at | 计数 | Guard 处置 |
|---|------|------|------|--------------|------|------------|
| 0 | `task_queue.py:239-245` `mark_completed()` | 中心助手（被多处调用） | 否（调用方负责） | 是 | 否 | 吸收进统一 finalize |
| 1 | `dispatcher.py:1341-1363` `_run_task_lifecycle` | auto 任务 exit0 自然成功 | 是 | 是 | 是；唯一挂 PR review 回查的路径 | **经 Guard** |
| 2 | `dispatcher.py:1234-1248` 同函数 | 用户中断（exit -2/130） | 是 | 否 | 否 | bypass（用户主动行为） |
| 3 | `dispatcher.py:1055-1079` `_run_transient_retry` | 瞬时重试后恢复成功 | 是 | exit0 是/中断否 | exit0 是/中断否 | **经 Guard**（中断分支 bypass） |
| 4 | `dispatcher.py:1483-1514` `_run_pool_retry` | 号池轮换后成功 | 是 | 同上 | 同上 | **经 Guard**（中断分支 bypass） |
| 5 | `dispatcher.py:1753-1770` `_run_loop_iterations` | loop 信号 action==done | 是 | 是 | 是 | **经 Guard**（§15.2） |
| 6 | `dispatcher.py:1941-1958` `_run_goal_lifecycle` | goal evaluator achieved | 是 | 是 | 是 | **经 Guard**（§15.3） |
| 7 | `instance_manager.py:868-885` `_consume_output` chat 分支 + `dispatcher.py:3259-3272` PTY 轮询兜底 | chat turn 结束（exit0 或中断） | 是 | 分支不一 | 否 | **经 Guard**（§15.4，含短路） |
| 8 | `dispatcher.py:355-380` `_cleanup_stale_state` | 重启时 executing/in_progress 兜底 | 是 | 否 | 否 | **与 Guard 冲突**，恢复逻辑须先接管（14.2） |
| 9 | `api/tasks.py:389-394` `stop_session` | 用户手动停止且无进程 | 是 | 否 | 否 | bypass（人工操作） |
| 10 | `api/pr_monitor.py:315-350` webhook synchronize | 旧 PR review 任务被新 push 取代 | 是 | 否 | 否 | bypass（superseded 语义） |
| 11 | `ralph_loop.py:161-163` | legacy loop runner | 是 | 是 | 否 | legacy，随现状（不接 Guard） |
| 12 | `worker_relay.py:303-321` | worker status_change 镜像 | worker 端广播 | 否 | worker 本地计 | 镜像 worker 侧 Guard 结果，加共识守卫（§16） |
| 13 | `task_migrator.py:124` | 迁移失败回滚恢复原状态 | — | 否 | 否 | 非完成语义，不动 |

盘点同时暴露的既有不一致（阶段 1 顺带修复）：非 exit0 的完成路径普遍不写 `completed_at`、不计数；PR review 回查只挂在路径 1；分享/飞书通知挂在广播层而非写库处（`ws_broadcaster.py:70-77`）。
