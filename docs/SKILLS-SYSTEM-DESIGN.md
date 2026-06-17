# CCM Skills 系统设计方案 v4

> 基于 agent-ml-research、Hermes Agent、MiMo Code 的深度源码级调研，**针对 CCM 架构约束**做适配设计。
>
> v4 更新：所有借鉴点逐一对照 CCM 架构做适配，不再照搬任何单一项目。
>
> **实施状态**：Phase 1-3 已完成，Phase 4 部分完成（curator 定时任务已实现，skill_state 表未实现），Phase 5 部分完成（ccm_create_skill 已实现）。

## 1. CCM 架构约束（决定设计的前提）

| 约束 | 说明 | 影响 |
|------|------|------|
| 多 session 共享 cwd | 同一项目多个 task 用同一个 `project.local_path` | **不能在 cwd 写 skill 文件**（.mcp.json 冲突教训） |
| Pool 账号轮换 | 限流时自动换 `CLAUDE_CONFIG_DIR` | **不能把 skill 放 config_dir** |
| Worker rsync 单向 | Manager → Worker，Worker 不回写 | **Worker 上的进化数据需通过 relay 回传** |
| PTY 模式 | CC 在 PTY 里运行，CCM 通过 system prompt / MCP 注入 | **不依赖 CC 原生 skill 发现机制** |
| 常驻服务器 | CCM 是 FastAPI 服务，不是 CLI | **调度逻辑用 asyncio task，不是"启动时检查"** |
| 现有 MCP 机制 | per-task 临时 MCP config 已可用 | **Skill 工具通过 MCP server 暴露** |
| --append-system-prompt-file | Fable 5 已验证此注入路径 | **always-on skill 可复用此机制** |

## 2. 三个项目借鉴点的 CCM 适配

### 2.1 Skill 存储位置

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | repo `skills/global/` + workspace `~/.agent-ml-research/skills/projects/` + symlink 到 `.claude/skills/` | ✅ repo 内存储（rsync 自动部署到 Worker）<br>❌ symlink（CCM 不需要 CC 的 Read 来访问 skill）<br>❌ workspace 目录（CCM 用 DB 管理 per-task 状态） |
| Hermes | `~/.hermes/skills/`（完全独立命名空间） | ✅ 独立命名空间思路<br>❌ 用户 home 目录（CCM 是多用户服务，不是单用户 CLI） |
| MiMo | `~/.config/mimocode/skills/` + 读取所有框架目录 | ❌ 多框架兼容不需要（CCM 只用 CC）<br>✅ 项目级 skill 覆盖的思路 |

**CCM 决策**：

```
{CCM_REPO}/skills/                    # 全局 skills（版本控制，rsync 部署）
  monitor/SKILL.md                    # ✅ 已实现

{project}/.ccm/skills/                # 项目级 skills（可选，覆盖同名）
  custom-deploy/SKILL.md              # 项目特有的 skill

数据库 task.enabled_skills            # per-task 启用/禁用（✅ 已实现）

数据库 skill_lessons 表               # 进化教训（✅ 已实现，不写 skill 文件）
```

**为什么不写 skill 文件存教训**：agent-ml-research 把教训直接追加到 `.skill.md` 文件里。但 CCM 的 Worker 是只读部署（rsync 单向），Worker 上的教训写不回 Manager。改为 DB 存储，注入时动态合并。

### 2.2 Skill 注入机制

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | always-on 写入 CLAUDE.md + on-demand 列目录让 agent Read | ❌ CCM 不生成 CLAUDE.md<br>✅ always-on 注入思路 → 用 `--append-system-prompt-file`<br>✅ on-demand 思路 → 用 MCP 工具 `ccm_read_skill` |
| Hermes | 3-tier progressive disclosure（metadata → full → references） | ✅ 分层加载思路<br>❌ skill_view MCP 工具（CCM 可做类似的 `ccm_read_skill`） |
| MiMo | CC 原生 skill 发现（放 `.claude/skills/`） | ❌ 不能写 cwd/config_dir |

**CCM 决策**（✅ 已全部实现）：

```
L0：Skill 目录（system prompt 注入）
    所有 skill 的 name + description 写入 --append-system-prompt-file
    Budget：4000 chars（不含 always-on body）
    实现：skill_loader.build_skill_prompt_file()
    
L1：always-on skill body（system prompt 注入）
    ccm.always: true 的 skill 全文追加到同一个 system prompt file
    Budget：4000 chars body + 最多 10 个
    选择算法：priority 降序贪心，第一个不受 budget 限
    实现：skill_loader.select_always_skills()

L2：on-demand（MCP 工具）
    ccm_skills_server 的 ccm_read_skill(name) 工具
    Agent 在 L0 目录中看到 skill 后调用此工具获取全文
    返回内容包括 body + 关联的 lessons（从 DB 动态合并）
    同时记录 skill_usage（trigger_type="read"）

注入时机：
    instance_manager.launch() 时生成临时 system prompt file
    路径：/tmp/ccm-skills-{task_id}-{random}.md
    PTY 通过 --append-system-prompt-file 传给 CC
    非 PTY 通过 --append-system-prompt 传给 claude -p
```

### 2.3 命令系统

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | decorator 注册 + mode/visitor guard 自动应用 | ✅ 保留现有 COMMAND_REGISTRY decorator 模式 |
| Hermes | 每个 skill 自动注册为 /command | ❌ 未实现自动注册（命令仍在 command_registry.py 硬编码） |
| MiMo | /dream /distill 等内置命令 | ✅ $distill 作为内置命令 |

**CCM 实际实现**：

命令注册在 `command_registry.py` 中用 `register_command()` 静态注册，共 3 个命令：

```python
# 内置命令（command_registry.py 中 register_command）
$help     # always_available=True，调用 ccm_command_help 工具
$monitor  # required_skills={"monitor": True}，禁用内置 Monitor 工具
$distill  # always_available=True，调用 ccm_distill 工具

# ccm_command_help 工具返回命令列表时，同时返回 skill 目录
# ccm_read_skill 返回 skill 的 ccm.commands 字段供 agent 了解命令
```

> **与设计差异**：设计文档提到 skill 的 `ccm.commands` 启动时自动注册到 COMMAND_REGISTRY。实际未实现自动注册——skill 的 commands 信息通过 `ccm_command_help` 和 `ccm_read_skill` 返回给 agent，由 agent 自行理解和使用，而非注册为可解析的 `$command`。

### 2.4 自进化系统

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | 工具失败 → 反思 → 追加到 skill 文件 `## 经验沉淀` | ✅ 失败反思机制<br>❌ 写 skill 文件（Worker 回传问题）<br>→ **改为写 DB skill_lessons 表** |
| Hermes | Curator 定期整理（启动时+空闲时检查） | ✅ 生命周期管理<br>❌ "启动时检查"（CCM 是常驻服务）<br>→ **改为 asyncio 定时任务** |
| MiMo | Dream(7天) + Distill(30天)，启动时检查 last session | ✅ 周期性整理<br>❌ 启动检查<br>→ **改为后台 asyncio cron** |

**CCM 决策——进化数据存 DB 不存文件**（✅ 已实现）：

```sql
-- skill_lessons 表（✅ 已实现）
CREATE TABLE skill_lessons (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,          -- 关联的 skill
    lesson TEXT NOT NULL,              -- 教训内容
    source TEXT DEFAULT 'evolution',   -- evolution | distill | manual
    tool_name TEXT,                    -- 触发失败的工具
    worker_id INTEGER,                 -- 来自哪个 Worker（NULL=本机）
    lesson_hash TEXT UNIQUE,           -- MD5 去重
    created_at TIMESTAMP DEFAULT NOW
);

-- skill_usage 表（✅ 已实现）
CREATE TABLE skill_usage (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,        -- read | command | test
    task_id INTEGER,
    project_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW
);
```

> **与设计差异**：设计文档提到 `skill_state` 表（active/stale/archived 状态管理），实际未实现。curator 的生命周期判断直接通过 `skill_usage` 表查询 last_used 时间，不依赖独立状态表。

**为什么用 DB 不用文件**：
- Worker 上的教训通过 relay 事件回传，写入 Manager DB → 解决单向 rsync 问题
- 多 session 并发进化不会文件冲突 → DB 有事务保证
- 注入时动态合并 skill body + lessons → 灵活且一致

### 2.5 即时进化（失败反思）

✅ 已在 `skill_evolution.py` 实现。

```python
async def evolve_on_failure(tool_name, error, context, db, skills=None, worker_id=None):
    """工具失败 → 反思 → 教训写 DB。"""
    
    # 1. 定位关联 skill（3 级匹配：tools list → tag → name substring）
    related = find_related_skill(tool_name, skills)
    if not related:
        return False
    
    # 2. 节流（内存 cooldown dict，600 秒间隔）
    if _cooldowns.get(related, 0) > now - _COOLDOWN_SECONDS:
        return False
    
    # 3. LLM 反思（claude-haiku-4-5，三级 fallback：SDK → httpx → 启发式提取）
    lesson = await _reflect_on_failure(tool_name, error, context, existing)
    if lesson == "SKIP":  # 跳过瞬态错误（timeout/rate_limit/503 等）
        return False
    
    # 4. 去重（字符级 bigram 重叠 > 50% 判为重复）
    if is_duplicate(lesson, existing):
        return False
    
    # 5. 写 DB（lesson_hash 唯一约束防并发重复）
    db.add(SkillLesson(
        skill_name=related, lesson=lesson,
        tool_name=tool_name, worker_id=worker_id,
        lesson_hash=md5(f"{related}:{lesson}"),
    ))
```

**Worker 进化回传**：

> **与设计差异**：设计文档提到专用 `skill_evolution` 事件类型。实际实现更简洁——Worker relay 在处理 `tool_result` 事件时，若 `is_error=True`，直接在 Manager 侧调用 `evolve_on_failure()`，不需要 Worker 主动发起进化事件。

```python
# worker_relay.py _handle() 中
if event_type == "tool_result" and data.get("is_error") and data.get("tool_name"):
    await evolve_on_failure(
        tool_name=data["tool_name"],
        error=str(data.get("tool_output", ""))[:500],
        context=str(data.get("tool_input", ""))[:300],
        db=db, worker_id=worker.id,
    )
```

### 2.6 周期进化（Curator + Distill）

| 项目 | 触发方式 | CCM 适配 |
|------|---------|---------|
| agent-ml-research | 无周期进化 | — |
| Hermes | CLI 启动时 + idle 检查 | ❌ CCM 是常驻服务 |
| MiMo | 查 DB last session 时间 | ✅ 但改为 asyncio 定时任务 |

**CCM 实际实现**（✅ dispatcher._curator_loop）：

```python
class GlobalDispatcher:
    async def start(self):
        ...
        self._curator_task = asyncio.create_task(self._curator_loop())
    
    async def _curator_loop(self):
        """后台定期运行 Curator + Distill。每小时检查一次。"""
        _last_curator_run: datetime | None = None
        while self._running:
            await asyncio.sleep(3600)  # 每小时检查
            
            # 首次运行：种子时间戳，跳过（Hermes 模式）
            if _last_curator_run is None:
                _last_curator_run = now
                continue
            
            # 7 天间隔检查
            if hours_since < 168:
                continue
            
            # 空闲检查（无 executing/in_progress task）
            if executing > 0:
                continue
            
            # Phase 1: Curator 确定性整理（skill_usage 查 last_used）
            await run_curator(db)
            
            # Phase 2: Distill 提炼（每 30 天）
            if hours_since >= 720:
                await analyze_patterns(db)
```

**Curator 功能**（`skill_curator.py`）：
- `run_curator()`：检查所有 skill 的 usage/lesson 统计，标记 30 天未用的为 stale
- `log_skill_usage()`：记录 skill 使用事件
- `get_usage_report()`：返回使用统计报告

**Distill 功能**（`skill_distill.py`）：
- `analyze_patterns()`：分析近 N 天的 tool 使用模式
  - 高频工具统计
  - 高错误率工具 → 建议创建 skill
  - 工具组合共现分析 → 建议创建工作流 skill

> **与设计差异**：设计文档提到 `skill_state` 表的状态转换（active → stale → archived），实际未建此表，curator 只做统计报告和日志标记，不做持久化状态转换。

### 2.7 Progressive Disclosure 对照

| 项目 | L0（概览） | L1（详情） | L2（深度） |
|------|-----------|-----------|-----------|
| agent-ml-research | always body 注入 CLAUDE.md | on-demand 列目录，agent Read 文件 | references/ 子目录 |
| Hermes | skills_list 返回 metadata | skill_view 返回 SKILL.md | skill_view 返回 reference 文件 |
| MiMo | CC 原生发现 | CC 原生加载 | — |
| **CCM** | **system prompt file 列目录** | **MCP ccm_read_skill 返回 body+lessons** | **未实现（计划中）** |

CCM 的 L0 通过 `--append-system-prompt-file` 注入目录列表。L1 通过 MCP 工具 `ccm_read_skill(name)` 按需返回。这避免了写 cwd（MiMo 方式）或写 CLAUDE.md（agent-ml-research 方式）。

## 3. Skill 定义格式

保持 SKILL.md 标准格式，CCM 扩展字段适配上述决策：

```yaml
---
name: monitor
description: >
  当用户需要持续监控进程、端口、日志、构建状态等后台任务时使用。
  适用于需要定期检查并汇报状态的长时间运行监控场景。
when_to_use: >
  用户要求监控某个进程、服务端口、日志文件、构建状态，
  或者需要后台定期检查某个条件并汇报时激活。
disallowed-tools:
  - Monitor

ccm:
  always: false
  priority: 8
  version: 1
  tags: [monitoring, sub-agent]
  tools: [create_monitor, check_monitors, stop_monitor]  # 进化定位用
  commands:
    - name: monitor
      pattern: "$monitor {task_description}"
      description: "创建后台监控子 agent"
  # 以下字段已被解析但暂未使用
  # roles: []        # 按角色过滤
  # modes: []        # 按模式过滤
  # triggers: []     # 自然语言触发匹配（未实现）
---

## 监控规则
...

## Lessons Learned
<!-- 注入时从 DB skill_lessons 表动态合并，不写在文件里 -->
<!-- 文件里的此区域仅供人工编辑的静态教训 -->
```

## 4. 完整注入流程

```
Task 启动
  │
  ├─ 1. discover_skills()                          [skill_loader.py]
  │     扫描 {CCM_REPO}/skills/ + {project}/.ccm/skills/
  │     过滤 roles/modes
  │     和 task.enabled_skills 合并
  │
  ├─ 2. build_skill_prompt_file()                  [skill_loader.py]
  │     生成 /tmp/ccm-skills-{task_id}-{random}.md
  │     ├─ L0 目录：所有 skill 的 name + description
  │     └─ L1 body：always:true skills 全文（budget 控制）
  │
  ├─ 3. 生成 /tmp/ccm_mcp_{task_id}.json           [mcp_config.py]
  │     ├─ ccm_skills_server（9 个 MCP 工具）
  │     └─ pty-bridge（PTY 模式）
  │
  ├─ 4. 启动 CC                                    [instance_manager.py]
  │     claude --append-system-prompt-file /tmp/ccm-skills-{task_id}-xxx.md
  │           --mcp-config /tmp/ccm_mcp_{task_id}.json
  │           --disallowedTools {skill_disallowed_tools}
  │
  └─ 5. 运行中
        ├─ Agent 调用 ccm_read_skill(name) → L2 on-demand 加载 + usage 记录
        ├─ Agent 用 $command → 加载关联 skill + 执行命令逻辑
        ├─ 工具失败 → evolve_on_failure() → lesson 写 DB
        ├─ ccm_read_skill 返回时动态合并 DB lessons
        └─ Agent 可用 ccm_create_skill 创建新 skill
```

## 5. MCP 工具清单

`ccm_skills_server.py` 提供 9 个 MCP 工具：

| 工具 | 说明 | 状态 |
|------|------|------|
| `ccm_command_help` | 列出所有命令 + skill 目录 | ✅ |
| `ccm_read_skill(name)` | 读取 skill 全文 + DB lessons | ✅ |
| `ccm_create_skill(name, ...)` | 创建新 SKILL.md 文件 | ✅ |
| `ccm_distill(days)` | 分析使用模式，提炼新 skill 建议 | ✅ |
| `ccm_enable_skill(name)` | 为当前 task 启用 skill | ✅ |
| `ccm_disable_skill(name)` | 为当前 task 禁用 skill（内置命令不可禁用） | ✅ |
| `create_monitor(desc, ...)` | 创建监控子 agent session | ✅ |
| `check_monitors()` | 查看当前 task 所有 monitor 状态 | ✅ |
| `stop_monitor(id)` | 停止指定 monitor | ✅ |

## 6. API 端点

| 端点 | 说明 | 状态 |
|------|------|------|
| `GET /api/system/skills` | 列出所有可用 skill（from SKILL.md） | ✅ |
| `GET /api/system/skills/usage` | skill 使用统计 | ✅ |
| `POST /api/system/skills/curator` | 手动触发 curator 整理 | ✅ |
| `POST /api/system/skills/distill` | 手动触发 distill 分析 | ✅ |
| `POST /api/tasks/{id}/monitor-sessions` | 创建 monitor（需 skill enabled + task active） | ✅ |
| `GET /api/tasks/{id}/monitor-sessions` | 列出 monitor sessions | ✅ |
| `GET /api/tasks/{id}/monitor-sessions/{mid}` | 获取单个 monitor | ✅ |
| `DELETE /api/tasks/{id}/monitor-sessions/{mid}` | 停止 monitor | ✅ |

## 7. Worker 同步方案

```
Manager                          Worker
  │                                │
  ├─ skills/ 在 CCM repo 里        ├─ rsync 自动获得 skills/
  │                                │
  ├─ skill_lessons DB              │  工具失败事件
  │                                │     ↓
  │  ← relay tool_result ←──────── │  relay 中继 tool_result（is_error=true）
  │     ↓                          │     Manager 侧直接调用 evolve_on_failure()
  │  写入 Manager DB               │     不需要 Worker 主动发起进化
  │                                │
  ├─ ccm_read_skill(name)          ├─ ccm_read_skill(name)
  │  → body + Manager DB lessons   │  → body + Worker 本地 DB lessons
  │                                │  （Worker 可能缺少 Manager 的 lessons，
  │                                │   但核心 skill body 一致）
```

## 8. 实施状态

### Phase 1：Skill 文件化 + 发现机制 ✅ 已完成
- [x] monitor 迁移为 `skills/monitor/SKILL.md`
- [x] 实现 `discover_skills()` 扫描 skills/ 目录
- [x] 生成 `/tmp/ccm-skills-{task_id}-xxx.md` 注入 system prompt
- [x] 前端从 `/api/system/skills` API 获取列表
- [x] DB migration：skill_lessons + skill_usage 表

### Phase 2：MCP 扩展 + 命令注册 ✅ 已完成
- [x] `ccm_read_skill(name)` MCP 工具
- [x] `ccm_command_help` 返回命令 + skill 目录
- [x] `ccm_read_skill` 返回时动态合并 DB lessons
- [x] `$help` / `$monitor` / `$distill` 命令注册
- [ ] ~~triggers 子串匹配~~（字段已解析，触发未实现——优先级低）
- [ ] ~~skill 的 ccm.commands 自动注册到 COMMAND_REGISTRY~~（改为通过 ccm_command_help 暴露给 agent）

### Phase 3：即时进化 ✅ 已完成
- [x] `evolve_on_failure()`：失败反思 → 教训写 DB
- [x] Worker 进化通过 relay 的 tool_result 事件触发（Manager 侧执行）
- [x] 使用追踪 `log_skill_usage()` 写 DB
- [x] API skill 使用统计

### Phase 4：周期进化 ⚠️ 部分完成
- [x] Curator asyncio 定时任务（`dispatcher._curator_loop`，每 7 天）
- [x] Distill 分析（每 30 天）
- [x] 首次延迟 + 空闲检查
- [ ] ~~skill_state 表的状态转换~~（未建表，curator 直接查 skill_usage 判断）
- [ ] ~~审批门控 UI~~

### Phase 5：高级功能 ⚠️ 部分完成
- [x] `ccm_create_skill` MCP 工具（agent 可创建新 skill）
- [x] `ccm_distill` MCP 工具（agent 可分析使用模式）
- [ ] references/ 子目录 L2 加载
- [ ] Plugin 热加载
- [ ] Skill 依赖图

## 9. 关键设计决策总结

| 决策 | 选择 | 对比参考项目 | 理由 |
|------|------|------------|------|
| Skill 存储 | CCM repo `skills/` | agent-ml-research: repo+workspace<br>Hermes: ~/.hermes/<br>MiMo: ~/.config/mimocode/ | Worker rsync 自动部署；不依赖用户 home |
| 进化数据 | DB 表 | agent-ml-research: 写 skill 文件<br>Hermes: 文件+metadata JSON | Worker 回传需要；并发安全；不改 skill 文件 |
| 注入方式 | system prompt file + MCP | agent-ml-research: CLAUDE.md<br>Hermes: MCP 工具<br>MiMo: CC 原生发现 | 不写 cwd（多 session 安全）；复用 Fable 5 路径 |
| 周期任务调度 | asyncio 定时任务 | Hermes: CLI 启动检查<br>MiMo: session 时间差 | CCM 是常驻服务不是 CLI |
| 使用追踪 | DB 表 | agent-ml-research: JSONL<br>Hermes: JSON per-skill | Worker 同步；SQL 聚合查询更方便 |
| 去重算法 | 字符级 bigram | agent-ml-research: 词重叠 60% | 中文兼容（词重叠对中文无效） |
| 命令注册 | 硬编码 + MCP 暴露 | agent-ml-research: 纯 decorator<br>Hermes: 纯 skill 注册 | 简单可控；agent 通过 ccm_command_help 获取 |
| Worker 进化回传 | relay 侧直接调用 | 设计文档: 专用事件 | 更简洁，不需要 Worker 改代码 |

## 10. 参考来源

| 项目 | 主要借鉴（适配后） |
|------|-----------------|
| agent-ml-research | Skill 格式 + budget 控制 + 3 级进化定位 + 使用追踪 + Worker budget 分离 |
| Hermes Agent | Curator 生命周期 + 首次延迟 + pinning 概念 |
| MiMo Code | Distill 分析 + 项目年龄检查 + CSO 描述原则 + 调度间隔设计 |
| Claude Code | SKILL.md 标准格式 + --append-system-prompt-file 注入路径 |
