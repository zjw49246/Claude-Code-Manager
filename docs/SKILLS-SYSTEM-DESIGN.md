# CCM Skills 系统设计方案 v4

> 基于 agent-ml-research、Hermes Agent、MiMo Code 的深度源码级调研，**针对 CCM 架构约束**做适配设计。
>
> v4 更新：所有借鉴点逐一对照 CCM 架构做适配，不再照搬任何单一项目。

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
  code-review/SKILL.md
  monitor/SKILL.md
  help/SKILL.md

{project}/.ccm/skills/                # 项目级 skills（可选，覆盖同名）
  custom-deploy/SKILL.md              # 项目特有的 skill

数据库 task.enabled_skills            # per-task 启用/禁用（现有机制）

数据库 skill_lessons 表               # 进化教训（不写 skill 文件，避免 Worker 回传问题）
```

**为什么不写 skill 文件存教训**：agent-ml-research 把教训直接追加到 `.skill.md` 文件里。但 CCM 的 Worker 是只读部署（rsync 单向），Worker 上的教训写不回 Manager。改为 DB 存储，注入时动态合并。

### 2.2 Skill 注入机制

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | always-on 写入 CLAUDE.md + on-demand 列目录让 agent Read | ❌ CCM 不生成 CLAUDE.md<br>✅ always-on 注入思路 → 用 `--append-system-prompt-file`<br>✅ on-demand 思路 → 用 MCP 工具 `ccm_read_skill` |
| Hermes | 3-tier progressive disclosure（metadata → full → references） | ✅ 分层加载思路<br>❌ skill_view MCP 工具（CCM 可做类似的 `ccm_read_skill`） |
| MiMo | CC 原生 skill 发现（放 `.claude/skills/`） | ❌ 不能写 cwd/config_dir |

**CCM 决策**：

```
L0：Skill 目录（system prompt 注入）
    所有 skill 的 name + description 写入 --append-system-prompt-file
    Budget：4000 chars（不含 always-on body）
    
L1：always-on skill body（system prompt 注入）
    ccm.always: true 的 skill 全文追加到同一个 system prompt file
    Budget：4000 chars body + 最多 10 个
    选择算法：priority 降序贪心，第一个不受 budget 限

L2：on-demand（MCP 工具）
    ccm_skills_server 新增 ccm_read_skill(name) 工具
    Agent 在 L0 目录中看到 skill 后调用此工具获取全文
    返回内容包括 body + 关联的 lessons（从 DB 动态合并）

注入时机：
    instance_manager.launch() 时生成临时 system prompt file
    路径：/tmp/ccm_skills_{task_id}.md
    PTY 通过 --append-system-prompt-file 传给 CC
    非 PTY 通过 --append-system-prompt 传给 claude -p
```

### 2.3 命令系统

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | decorator 注册 + mode/visitor guard 自动应用 | ✅ 保留现有 COMMAND_REGISTRY decorator 模式<br>✅ 增加 guard 自动应用 |
| Hermes | 每个 skill 自动注册为 /command | ✅ skill 的 ccm.commands 自动注册<br>❌ 所有 skill 都注册为命令（有的 skill 不需要命令） |
| MiMo | /dream /distill 等内置命令 | ✅ 系统维护命令（$curator $distill）作为内置命令 |

**CCM 决策**：

```python
# 启动时自动注册
def register_all_commands():
    # 1. 内置命令（Python 代码）
    # $help, $status, $stop — 现有 COMMAND_REGISTRY
    
    # 2. Skill 自动命令
    for skill in discover_skills():
        for cmd in skill.ccm.get("commands", []):
            COMMAND_REGISTRY.register_skill_command(cmd, skill)
    
    # 3. 系统维护命令
    # $curator — 手动触发 curator 整理
    # $distill — 手动触发 distill 提炼
```

### 2.4 自进化系统

| 项目 | 做法 | CCM 适配 |
|------|------|---------|
| agent-ml-research | 工具失败 → 反思 → 追加到 skill 文件 `## 经验沉淀` | ✅ 失败反思机制<br>❌ 写 skill 文件（Worker 回传问题）<br>→ **改为写 DB skill_lessons 表** |
| Hermes | Curator 定期整理（启动时+空闲时检查） | ✅ 生命周期管理<br>❌ "启动时检查"（CCM 是常驻服务）<br>→ **改为 asyncio 定时任务** |
| MiMo | Dream(7天) + Distill(30天)，启动时检查 last session | ✅ 周期性整理<br>❌ 启动检查<br>→ **改为后台 asyncio cron** |

**CCM 决策——进化数据存 DB 不存文件**：

```sql
-- 新表：skill_lessons（进化教训）
CREATE TABLE skill_lessons (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,          -- 关联的 skill
    lesson TEXT NOT NULL,              -- 教训内容
    source TEXT DEFAULT 'evolution',   -- evolution | distill | manual
    tool_name TEXT,                    -- 触发失败的工具
    worker_id INTEGER,                -- 来自哪个 Worker（NULL=本机）
    created_at TIMESTAMP DEFAULT NOW,
    UNIQUE(skill_name, lesson_hash)    -- 去重用
);

-- 新表：skill_usage（使用追踪）
CREATE TABLE skill_usage (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,
    trigger_type TEXT NOT NULL,        -- inject | read | command | trigger
    task_id INTEGER,
    project_id INTEGER,
    created_at TIMESTAMP DEFAULT NOW
);

-- 新表：skill_state（生命周期状态）
CREATE TABLE skill_state (
    skill_name TEXT PRIMARY KEY,
    state TEXT DEFAULT 'active',       -- active | stale | archived
    pinned BOOLEAN DEFAULT FALSE,
    created_by TEXT DEFAULT 'system',  -- system | agent | distill
    last_used_at TIMESTAMP,
    state_changed_at TIMESTAMP,
    absorbed_into TEXT                 -- 合并追踪
);
```

**为什么用 DB 不用文件**：
- Worker 上的教训通过 relay 事件回传，写入 Manager DB → 解决单向 rsync 问题
- 多 session 并发进化不会文件冲突 → DB 有事务保证
- 注入时动态合并 skill body + lessons → 灵活且一致

### 2.5 即时进化（失败反思）

```python
async def evolve_on_failure(tool_name, error, context, task_id, skill_registry):
    """适配 CCM：教训写 DB 而非文件。"""
    
    # 1. 定位关联 skill（agent-ml-research 3 级匹配）
    related = find_related_skill(tool_name, skill_registry)
    if not related:
        return
    
    # 2. 节流（DB 查询代替文件时间戳）
    recent = await db.execute(
        select(SkillLesson)
        .where(SkillLesson.skill_name == related.name)
        .where(SkillLesson.created_at > now() - timedelta(seconds=600))
    )
    if recent.first():
        return
    
    # 3. LLM 反思（同 agent-ml-research）
    lesson = await reflect_on_failure(tool_name, error, context, model="claude-haiku-4-5")
    if lesson == "SKIP":
        return
    
    # 4. 去重（字符级 bigram，适配中文）
    existing = await get_recent_lessons(related.name, limit=5)
    if is_duplicate_bigram(lesson, existing, threshold=0.5):
        return
    
    # 5. 写 DB（不写文件，Worker 通过 relay 调用此函数）
    await db.add(SkillLesson(
        skill_name=related.name,
        lesson=lesson,
        tool_name=tool_name,
        worker_id=current_worker_id,  # None=本机
    ))
    await db.commit()
```

**Worker 进化回传**：
```python
# Worker 上的失败反思结果通过 relay 事件发回 Manager
# relay._handle() 新增 skill_evolution 事件类型
if event_type == "skill_evolution":
    await db.add(SkillLesson(
        skill_name=data["skill_name"],
        lesson=data["lesson"],
        tool_name=data["tool_name"],
        worker_id=worker.id,
    ))
```

### 2.6 周期进化（Curator + Distill）

| 项目 | 触发方式 | CCM 适配 |
|------|---------|---------|
| agent-ml-research | 无周期进化 | — |
| Hermes | CLI 启动时 + idle 检查 | ❌ CCM 是常驻服务 |
| MiMo | 查 DB last session 时间 | ✅ 但改为 asyncio 定时任务 |

**CCM 决策**：

```python
# dispatcher.py 中增加后台 cron 任务
class GlobalDispatcher:
    async def start(self):
        ...
        self._curator_task = asyncio.create_task(self._curator_loop())
    
    async def _curator_loop(self):
        """后台定期运行 Curator + Distill。"""
        while self._running:
            await asyncio.sleep(3600)  # 每小时检查一次
            
            if not self._should_run_curator():
                continue
            
            # 只在空闲时运行（无 executing task）
            if any(t.status == "executing" for t in active_tasks):
                continue
            
            await self._run_curator()   # Phase 1: 确定性整理
            await self._run_distill()   # Phase 2: 提炼新 skill
    
    def _should_run_curator(self):
        """融合 Hermes 首次延迟 + MiMo 项目年龄检查。"""
        last_run = self._get_last_curator_run()  # DB 查询
        if last_run is None:
            self._set_last_curator_run(now())  # 首次种子（Hermes）
            return False
        if (now() - last_run).days < 7:
            return False
        # 项目年龄检查（MiMo）：有项目至少 7 天历史
        oldest_task = self._get_oldest_task()
        if oldest_task and (now() - oldest_task.created_at).days < 7:
            return False
        return True
```

### 2.7 Progressive Disclosure 对照

| 项目 | L0（概览） | L1（详情） | L2（深度） |
|------|-----------|-----------|-----------|
| agent-ml-research | always body 注入 CLAUDE.md | on-demand 列目录，agent Read 文件 | references/ 子目录 |
| Hermes | skills_list 返回 metadata | skill_view 返回 SKILL.md | skill_view 返回 reference 文件 |
| MiMo | CC 原生发现 | CC 原生加载 | — |
| **CCM** | **system prompt file 列目录** | **MCP ccm_read_skill 返回 body+lessons** | **MCP 返回 references（Phase 5）** |

CCM 的 L0 通过 `--append-system-prompt-file` 注入目录列表。L1 通过 MCP 工具 `ccm_read_skill(name)` 按需返回。这避免了写 cwd（MiMo 方式）或写 CLAUDE.md（agent-ml-research 方式）。

## 3. Skill 定义格式

保持 SKILL.md 标准格式，但 CCM 扩展字段需适配上述决策：

```yaml
---
name: code-review
description: >
  当用户要求审查代码、提交 PR 前检查、或评估代码质量时使用。
when_to_use: >
  用户提到"审查"、"review"、"检查代码"时激活。
arguments:
  - name: target
    description: 要审查的文件或目录
allowed-tools:
  - Read
  - Bash(grep *)

ccm:
  always: false
  priority: 5
  version: 3
  tags: [quality, review]
  roles: []
  modes: []
  tools: [Read, Bash]           # 进化定位用
  commands:
    - name: review
      pattern: "$review {target}"
      description: "审查指定文件或 PR"
  triggers:
    - "帮我审查"
    - "review this"
---

## 审查要点
...

## Lessons Learned
<!-- 注入时从 DB skill_lessons 表动态合并，不写在文件里 -->
<!-- 文件里的此区域仅供人工编辑的静态教训 -->
```

## 4. 完整注入流程

```
Task 启动
  │
  ├─ 1. discover_skills()
  │     扫描 {CCM_REPO}/skills/ + {project}/.ccm/skills/
  │     过滤 roles/modes
  │     和 task.enabled_skills 合并
  │
  ├─ 2. 生成 /tmp/ccm_skills_{task_id}.md
  │     ├─ L0 目录：所有 skill 的 name + description
  │     └─ L1 body：always:true skills 全文（budget 控制）
  │
  ├─ 3. 生成 /tmp/ccm_mcp_{task_id}.json
  │     ├─ ccm_skills_server（现有 + 新增 ccm_read_skill）
  │     └─ pty-bridge（PTY 模式）
  │
  ├─ 4. 启动 CC
  │     claude --append-system-prompt-file /tmp/ccm_skills_{task_id}.md
  │           --mcp-config /tmp/ccm_mcp_{task_id}.json
  │           --dangerously-load-development-channels server:pty-bridge
  │
  └─ 5. 运行中
        ├─ Agent 调用 ccm_read_skill(name) → L2 on-demand 加载
        ├─ Agent 用 $command → 加载关联 skill + 执行命令逻辑
        ├─ 工具失败 → evolve_on_failure() → lesson 写 DB
        └─ ccm_read_skill 返回时动态合并 DB lessons
```

## 5. Worker 同步方案

```
Manager                          Worker
  │                                │
  ├─ skills/ 在 CCM repo 里        ├─ rsync 自动获得 skills/
  │                                │
  ├─ skill_lessons DB              │  工具失败 → evolve_on_failure()
  │                                │     ↓
  │  ← relay "skill_evolution" ←── │  relay 事件回传 lesson
  │     ↓                          │
  │  写入 Manager DB               │
  │                                │
  ├─ ccm_read_skill(name)          ├─ ccm_read_skill(name)
  │  → body + Manager DB lessons   │  → body + Worker 本地 DB lessons
  │                                │  （Worker 可能缺少 Manager 的 lessons，
  │                                │   但核心 skill body 一致）
```

## 6. 现有系统改造路径

### Phase 1：Skill 文件化 + 发现机制（2-3 天）
- 将 help/workflows/monitor 迁移为 `{CCM_REPO}/skills/` 下的 SKILL.md
- 实现 `discover_skills()` 替代硬编码 `ALL_TOOLS` 和 `COMMAND_REGISTRY`
- 生成 `/tmp/ccm_skills_{task_id}.md` 注入 system prompt
- 前端从 `/api/skills` API 获取列表（不再硬编码）
- DB migration：skill_lessons + skill_usage + skill_state 表

### Phase 2：MCP 扩展 + 命令自动注册（2-3 天）
- ccm_skills_server 新增 `ccm_read_skill(name)` 工具
- Skill 的 `ccm.commands` 启动时自动注册到 COMMAND_REGISTRY
- `ccm_read_skill` 返回时动态合并 DB lessons
- triggers 子串匹配（简单实现，top 3 限制）

### Phase 3：即时进化（2-3 天）
- `evolve_on_failure()`：失败反思 → 教训写 DB
- Worker 进化通过 relay 回传
- 使用追踪写 DB（替代 JSONL 文件，避免 Worker 同步问题）
- 前端 skill 使用统计

### Phase 4：周期进化（3-5 天）
- Curator asyncio 定时任务（dispatcher 内）
- 确定性状态转换（active → stale → archived）
- Distill 6 阶段流程
- 审批门控 UI
- 首次延迟 + 项目年龄检查

### Phase 5：高级功能（后续）
- 优化环（对比分析）
- Skill Creator
- references/ 子目录 L2 加载
- Plugin 热加载
- Skill 依赖图

## 7. 关键设计决策总结

| 决策 | 选择 | 对比参考项目 | 理由 |
|------|------|------------|------|
| Skill 存储 | CCM repo `skills/` | agent-ml-research: repo+workspace<br>Hermes: ~/.hermes/<br>MiMo: ~/.config/mimocode/ | Worker rsync 自动部署；不依赖用户 home |
| 进化数据 | DB 表 | agent-ml-research: 写 skill 文件<br>Hermes: 文件+metadata JSON | Worker 回传需要；并发安全；不改 skill 文件 |
| 注入方式 | system prompt file + MCP | agent-ml-research: CLAUDE.md<br>Hermes: MCP 工具<br>MiMo: CC 原生发现 | 不写 cwd（多 session 安全）；复用 Fable 5 路径 |
| 周期任务调度 | asyncio 定时任务 | Hermes: CLI 启动检查<br>MiMo: session 时间差 | CCM 是常驻服务不是 CLI |
| 使用追踪 | DB 表 | agent-ml-research: JSONL<br>Hermes: JSON per-skill | Worker 同步；SQL 聚合查询更方便 |
| 去重算法 | 字符级 bigram | agent-ml-research: 词重叠 60% | 中文兼容（词重叠对中文无效） |
| 命令注册 | decorator + skill 自动注册 | agent-ml-research: 纯 decorator<br>Hermes: 纯 skill 注册 | 兼容现有内置命令 + 支持 skill 扩展 |
| 触发匹配 | 子串匹配 + top 3 | 三个项目都不做自然语言触发 | CCM 独有需求，简单实现先 |

## 8. 参考来源

| 项目 | 主要借鉴（适配后） |
|------|-----------------|
| agent-ml-research | Skill 格式 + budget 控制 + 3 级进化定位 + 使用追踪 + Worker budget 分离 |
| Hermes Agent | Curator 生命周期 + absorbed_into + 原子写入 + pinning + 首次延迟 + scaffolding extraction |
| MiMo Code | Distill 6 阶段 + 项目年龄检查 + CSO 描述原则 + 调度间隔设计 |
| Claude Code | SKILL.md 标准格式 + --append-system-prompt-file 注入路径 |
