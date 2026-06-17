# CCM Skills 系统设计方案 v3

> 基于 agent-ml-research、Hermes Agent、MiMo Code、Claude Code 原生 skills、SkillEvolver 等项目的深度源码级调研，融合设计。
>
> v3 更新：设计验证——修正 4 个关键问题、7 个重要缺陷、5 个改进点。

## 1. 设计目标

- Skills 可自动发现、按需加载、自主进化
- 兼容 [Agent Skills 开放标准](https://agentskills.io)（Claude Code / Hermes / MiMo / Codex CLI 共用的 SKILL.md 格式）
- Commands 和 Skills 互补但不混淆
- 前端从 API 动态获取，不再硬编码
- 三环自进化：即时反思 → 周期整理 → 按需优化
- Worker 场景下的 skill 注入有独立 budget

## 2. 现状问题

| 问题 | 影响 |
|------|------|
| Skills 硬编码在 3 处（后端 registry、前端 ALL_TOOLS、测试） | 新增 skill 要改 3 个文件 |
| ccm_skills_server 固定 4 个 MCP 工具 | 无法动态扩展 |
| Skill 只是"开关"，没有知识内容 | Claude 不知道 skill 能做什么 |
| 无自进化能力 | agent 不能从经验中学习 |
| Monitor 特殊处理 | 代码耦合重 |

## 3. Skill 定义格式

采用 Agent Skills 开放标准的 `SKILL.md` 格式（兼容 Claude Code 原生），扩展 CCM 特有字段。

### 3.1 目录结构

三层层级（参考 agent-ml-research 的 global/project/role 架构）：

```
~/.claude/skills/                          # 用户级（全局）
  code-review/
    SKILL.md
    scripts/                               # 可选脚本（Hermes 标准）
    references/                            # 可选参考文档（按需加载）
  monitor/
    SKILL.md

{project}/.claude/skills/                  # 项目级（覆盖同名全局 skill）
  deploy/
    SKILL.md

{project}/.claude/sessions/{role}/skills/  # 角色级（最高优先级）
  worker-specific/
    SKILL.md
```

**覆盖规则**（参考 Claude Code scope hierarchy）：角色级 > 项目级 > 全局。同名 skill，高层级覆盖低层级。

### 3.2 SKILL.md 格式

```yaml
---
# === Agent Skills 标准字段 ===
name: code-review
# 描述原则（MiMo CSO 洞察）：描述 WHEN to use，不要描述 WHAT it does
# 测试表明 agent 会直接根据 description 行动而不读 skill body
description: >
  当用户要求审查代码、提交 PR 前检查、或评估代码质量时使用。
  适用于安全审计、性能分析、规范检查。
when_to_use: >
  用户提到"审查"、"review"、"检查代码"、"code quality"时激活。
arguments:
  - name: target
    description: 要审查的文件或目录
allowed-tools:
  - Read
  - Bash(grep *)
  - Bash(git diff *)

# === CCM 扩展字段 ===
ccm:
  always: false              # 是否自动注入 system prompt
  priority: 5                # 注入优先级（budget 满时决定取舍）
  scope: global              # global | project
  version: 3
  heavy: false               # >5KB 标记（UI 提示 token 开销）
  tags: [quality, review]
  roles: []                  # 空=所有角色可用
  modes: []                  # 项目类型过滤（空=所有类型）
  tools: [Read, Bash]        # 关联工具（用于失败反思定位 skill）
  commands:                  # 自动注册为 CCM 命令
    - name: review
      pattern: "$review {target}"
      description: "审查指定文件或 PR"
  triggers:                  # 自然语言触发词
    - "帮我审查"
    - "review this"
    - "检查代码质量"
---

## 审查要点

### 安全性
- 检查 OWASP Top 10 漏洞
- SQL 注入、XSS、命令注入
- 敏感信息泄露

### 性能
- N+1 查询
- 无必要的同步操作
- 内存泄漏风险

### 代码规范
- 命名一致性
- 错误处理完整性
- 测试覆盖

## 经验教训
<!-- 自进化系统自动追加，最新的在最前面 -->
```

### 3.3 字段详解

| 字段 | 来源 | 说明 |
|------|------|------|
| `name`, `description` | Claude Code 标准 | description 限 1536 chars；**写 WHEN 不写 WHAT**（MiMo CSO） |
| `when_to_use` | Claude Code 标准 | 追加到 description 用于匹配，共享 1536 上限 |
| `arguments` | Claude Code 标准 | 命名参数，支持 `$name` / `$0` / `$1` 替换 |
| `allowed-tools` / `disallowed-tools` | Claude Code 标准 | Skill 激活时的工具权限变更 |
| `ccm.always` | agent-ml-research | 是否自动注入 system prompt |
| `ccm.priority` | agent-ml-research | Budget 满时的取舍优先级（高优先） |
| `ccm.heavy` | agent-ml-research | >5KB 警告标记 |
| `ccm.tools` | agent-ml-research | **关联工具列表——进化系统用此字段确定性定位失败关联的 skill**，避免模糊匹配 |
| `ccm.commands` | Hermes | 自动注册为 CCM 命令 |
| `ccm.triggers` | 新增 | 自然语言触发词 |
| `ccm.roles` | agent-ml-research | 角色过滤（空=所有），在 discovery 阶段过滤 |
| `ccm.modes` | agent-ml-research | 项目类型过滤（空=所有） |
| `ccm.tags` | 通用 | 分类标签（进化系统的第二级定位依据） |

## 4. 发现与注入机制

### 4.1 三层渐进加载

融合 agent-ml-research 的 budget 控制 + Hermes 的 Progressive Disclosure + Claude Code 的 context budget：

```
Session 启动
  │
  ├─ L0：Skill 目录（始终在 system prompt 中）
  │     所有 skill 的 name + description（一行摘要）
  │     Budget：context window 的 1%（Claude Code 标准）
  │     溢出时按 usage 频率排序，低频 skill 的 description 降级为仅 name
  │
  ├─ L1：always 注入
  │     ccm.always: true 的 skill 全文注入 system prompt
  │     Budget：max_always_prompt_chars（默认 4000 chars）
  │     最多 max_always_in_prompt 个（默认 10）
  │     选择算法：按 priority 降序贪心填充
  │     ⚠️ 第一个 skill 即使超过 budget 也必须包含（agent-ml-research 设计）
  │
  ├─ L2：按需加载
  │     触发方式：
  │     ├─ 用户 $command → 加载关联 skill
  │     ├─ 自然语言匹配 ccm.triggers → 自动加载
  │     ├─ Agent 主动 Read skill 文件
  │     └─ 工具流检测（tracker 识别 Read .skill.md）
  │     加载后持久到 session 结束
  │     Context compaction 后重新附加前 5000 tokens/skill，总计上限 25000 tokens
  │
  └─ Worker 独立 budget
        Worker 场景下的 skill 注入使用独立 budget
        max_worker_skill_chars：6000（比主 agent 的 4000 更宽松）
        （参考 agent-ml-research：Worker 需要更具体的操作指南）
```

### 4.2 发现流程

```python
def discover_skills(config_dir, project_dir, role=None, mode=None):
    """扫描 skill 目录，构建 registry。"""
    skills = {}

    # 全局 skills
    user_dir = Path(config_dir) / "skills"
    for skill_dir in user_dir.iterdir():
        if (skill_dir / "SKILL.md").exists():
            skills[skill_dir.name] = parse_skill(skill_dir / "SKILL.md")

    # 项目级 skills（覆盖同名）
    proj_skill_dir = Path(project_dir) / ".claude" / "skills"
    for skill_dir in proj_skill_dir.iterdir():
        if (skill_dir / "SKILL.md").exists():
            skills[skill_dir.name] = parse_skill(skill_dir / "SKILL.md")

    # 角色级 skills（最高优先级覆盖）
    if role:
        role_dir = Path(project_dir) / ".claude" / "sessions" / role / "skills"
        for skill_dir in role_dir.iterdir():
            if (skill_dir / "SKILL.md").exists():
                skills[skill_dir.name] = parse_skill(skill_dir / "SKILL.md")

    # 过滤：role、mode（在 discovery 阶段过滤，filtered skills 完全不可见）
    if role:
        skills = {k: v for k, v in skills.items()
                  if not v.ccm.roles or role in v.ccm.roles}
    if mode:
        skills = {k: v for k, v in skills.items()
                  if not v.ccm.modes or mode in v.ccm.modes}

    return skills
```

### 4.3 Budget 选择算法

```python
def select_skills_within_budget(skills, max_count=10, max_chars=4000):
    """贪心选择 always:true 的 skills，按 priority 降序。
    
    关键设计（agent-ml-research）：
    第一个 skill 即使超过 max_chars 也必须包含，防止高优先级大 skill 被静默丢弃。
    """
    always_skills = [s for s in skills.values() if s.ccm.always]
    sorted_skills = sorted(always_skills, key=lambda s: s.ccm.priority, reverse=True)
    
    selected = []
    total_chars = 0
    for skill in sorted_skills:
        body_len = len(skill.body)
        if len(selected) >= max_count:
            break
        if total_chars + body_len > max_chars and selected:  # 第一个不受限
            break
        selected.append(skill)
        total_chars += body_len
    return selected
```

## 5. 命令系统

### 5.1 命令与 Skill 的关系

两个独立但互补的系统（agent-ml-research 的核心设计理念）：

```
        ┌──────────────────────────────────────────────┐
        │             命令来源                           │
        │                                               │
        │  1. 内置命令（Python 代码，管理系统状态）         │
        │     $help, $status, $stop, $config             │
        │     → 不关联 skill，直接执行 Python 逻辑         │
        │     → decorator 注册，自带 mode/visitor guard   │
        │                                               │
        │  2. Skill 自动命令（SKILL.md ccm.commands）     │
        │     $review, $monitor, $deploy                 │
        │     → 加载 skill 内容作为上下文 + 执行逻辑       │
        │                                               │
        │  3. Agent 创建的命令（skill-creator 生成）        │
        │     $run-tests, $check-lint                     │
        │     → 完全由 SKILL.md 定义                      │
        └──────────────────────────────────────────────┘

互补关系示例：
  $review 命令 → 触发 Python 逻辑（收集 diff、设定上下文）
                + 加载 code-review.skill.md（审查标准和经验教训）
```

### 5.2 内置命令注册

```python
# 保留 decorator 模式（agent-ml-research），增加自动 guard
@command(
    name="help",
    patterns=(r"\$help\s*(.*)",),
    always_available=True,
    modes=(),          # 空=所有模式
    visitor=True,      # 是否允许只读模式使用
    category="system",
)
def handle_help(ctx: CommandContext) -> str:
    """列出所有可用命令和 skills。"""
    ...

# dispatcher 自动应用 guard（agent-ml-research 模式）：
# - visitor guard（只读模式限制）
# - mode guard（项目类型限制）
# handler 本身不需要检查这些条件
```

### 5.3 Skill 自动命令注册

Skill 的 `ccm.commands` 字段在 skill discovery 时自动注册到命令系统：

```python
for skill in skills.values():
    for cmd in skill.ccm.get("commands", []):
        register_skill_command(
            name=cmd["name"],
            pattern=cmd["pattern"],
            skill=skill,
            description=cmd.get("description", skill.description),
        )
```

## 6. 三环自进化系统

### 6.1 架构总览

```
┌──────────────────────────────────────────────────────────┐
│                    三环自进化                               │
│                                                           │
│  即时环（每次失败）           ← agent-ml-research            │
│  ├─ 触发：工具执行失败 / 任务失败                            │
│  ├─ 定位：skill.tools 确定性匹配 → tags → name 子串（3 级）  │
│  ├─ 反思：LLM (haiku) 分析失败原因                          │
│  ├─ 去重：词重叠 > 60% 跳过（快速路径，无 LLM 调用）          │
│  ├─ 瞬时错误：LLM 输出 "SKIP" 跳过网络超时等                 │
│  ├─ 节流：同一 skill 600s 内不重复（持久化到磁盘）            │
│  ├─ 安全：写入前创建 .bak 备份                              │
│  └─ 格式：时间戳前缀，最新在前，追加到 "## 经验教训" 区       │
│                                                           │
│  周期环（定期）               ← MiMo + Hermes Curator        │
│  ├─ 触发条件（Hermes 模式）：                                │
│  │   ├─ 距上次运行 >= interval_hours（默认 168h = 7 天）      │
│  │   ├─ 当前空闲 >= min_idle_hours（默认 2h）                │
│  │   └─ 首次运行延迟：种子 last_run_at 为当前时间（Hermes）    │
│  │       防止新安装立刻触发激进整理                            │
│  ├─ 项目年龄检查（MiMo）：                                   │
│  │   项目最早 session < interval_days 则跳过（太新没东西整理）  │
│  ├─ Phase 1 - 确定性整理（Hermes Curator）：                  │
│  │   ├─ 30 天未使用 → stale                                  │
│  │   ├─ 90 天未使用 → archived（移到 .archive/）              │
│  │   ├─ 使用后自动 stale → active（复活）                     │
│  │   └─ 永不自动删除                                         │
│  ├─ Phase 2 - Distill（MiMo 6 阶段）：                       │
│  │   ├─ 1. 定位数据源（对话历史、工具日志）                   │
│  │   ├─ 2. 盘点现有 skills/commands/agents                   │
│  │   ├─ 3. 从记忆中发现重复工作流                             │
│  │   ├─ 4. 在原始 trajectory 中确认（SQLite 查询）            │
│  │   ├─ 5. 筛选（出现 >= 2 次、输入稳定、有明确停止条件）     │
│  │   └─ 6. 仅创建高置信度资产 → 审批门控                     │
│  ├─ Phase 3 - Consolidate（Hermes Curator LLM 驱动）：        │
│  │   ├─ 扫描 agent 创建的 skills，寻找前缀聚类                │
│  │   ├─ 判断是否需要合并为 umbrella skill                     │
│  │   ├─ 合并时 references/templates/scripts 必须完整迁移      │
│  │   ├─ 记录 absorbed_into（Hermes 模式，追踪合并关系）       │
│  │   ├─ max_iterations = 8（防止无限循环）                    │
│  │   └─ 支持 dry-run 模式（只报告不执行）                     │
│  └─ 防重入：10 秒最小间隔（MiMo MIN_SPAWN_GAP）              │
│                                                           │
│  优化环（按需）               ← Hermes + SkillEvolver        │
│  ├─ 触发：人工执行 $optimize-skills                          │
│  ├─ 方法：对比分析（SkillEvolver）                           │
│  │   K=4 策略多样化并行执行                                   │
│  │   delta = features(成功) - features(失败)                  │
│  │   → 外科手术式修补（保留有效部分，补充缺失约束）            │
│  ├─ 验证：V=5 次独立测试对比                                  │
│  └─ 审计：9 项检查（内容泄露 1-6、部署失败 7-9）              │
└──────────────────────────────────────────────────────────┘
```

### 6.2 即时环：失败反思

```python
# core/skills/evolution.py（基于 agent-ml-research 源码）

COOLDOWN_FILE = "config/skill_evolution_cooldown.json"  # 持久化，进程重启不丢失
DEFAULT_COOLDOWN = 600  # 10 分钟

async def evolve_on_failure(
    tool_name: str,
    error: str,
    context: str,
    skill_registry: dict,
):
    """工具执行失败时，反思并追加经验到关联 skill。"""

    # 1. 定位关联 skill（3 级确定性匹配）
    #    Level 1: skill.ccm.tools 包含该工具名（最精确）
    #    Level 2: skill.ccm.tags 与工具名/错误类型匹配
    #    Level 3: skill.name 子串匹配（最模糊）
    related_skill = find_related_skill(tool_name, skill_registry)
    if not related_skill:
        return

    # 2. 节流检查（持久化到磁盘）
    cooldowns = load_json(COOLDOWN_FILE, {})
    if cooldowns.get(related_skill.name, 0) > time.time() - DEFAULT_COOLDOWN:
        return

    # 3. 提取现有教训（最近 5 条，避免 prompt 过长）
    existing_lessons = extract_lessons(related_skill.body, max_count=5)

    # 4. LLM 反思（用轻量模型）
    lesson = await reflect_on_failure(
        tool_name=tool_name,
        error=error,
        context=context,
        existing_lessons=existing_lessons,
        model="claude-haiku-4-5",
        # 明确指示：瞬时错误（网络超时等）输出 "SKIP"
    )
    if lesson == "SKIP":
        return

    # 5. 去重（快速路径，无 LLM 调用）
    #    Strip 日期前缀 [YYYY-MM-DD]，然后计算词重叠
    #    重叠 > 60% 则判定为重复
    if is_duplicate_by_word_overlap(lesson, existing_lessons, threshold=0.6):
        return

    # 6. 原子写入（备份 → 修改 → 更新 cooldown）
    backup_path = related_skill.path.with_suffix(".skill.md.bak")
    shutil.copy2(related_skill.path, backup_path)

    # 最新教训 prepend 到 "## 经验教训" 区
    prepend_lesson(related_skill.path, f"- [{today()}] **{lesson}**")

    # 更新 cooldown
    cooldowns[related_skill.name] = time.time()
    save_json(COOLDOWN_FILE, cooldowns)

    # 记录进化日志
    log_evolution(related_skill.name, tool_name, lesson)
```

### 6.3 周期环调度逻辑

```python
# 触发条件（融合 Hermes Curator + MiMo 调度）
def should_run_curator(
    last_run: datetime | None,
    idle_since: datetime,
    project_created: datetime,
    interval_hours: int = 168,
    min_idle_hours: int = 2,
) -> bool:
    now = datetime.utcnow()

    # 首次运行延迟（Hermes 设计）：
    # 新安装种子 last_run_at 为当前时间，延迟一个完整周期
    if last_run is None:
        save_last_run(now)  # seed
        return False

    # 项目年龄检查（MiMo 设计）：
    # 项目太新（< interval）没有足够历史可整理
    project_age_hours = (now - project_created).total_seconds() / 3600
    if project_age_hours < interval_hours:
        return False

    # 时间 + 空闲双条件
    hours_since_run = (now - last_run).total_seconds() / 3600
    hours_idle = (now - idle_since).total_seconds() / 3600
    return hours_since_run >= interval_hours and hours_idle >= min_idle_hours
```

### 6.4 使用追踪

```python
# core/skills/tracker.py（基于 agent-ml-research 源码）

import fcntl  # 跨进程文件锁

USAGE_LOG = "skill_usage.jsonl"
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB 轮转

def log_skill_usage(skill_name, trigger_type, project, session_id):
    """每次 skill 加载时记录。
    
    trigger_type:
      - "inject": always-on 自动注入
      - "read": agent 显式 Read 加载
      - "command": 通过 $command 触发
      - "trigger": 自然语言匹配触发
    """
    entry = {
        "timestamp": now().isoformat(),
        "skill": skill_name,
        "trigger": trigger_type,
        "project": project,
        "session": session_id,
    }
    with open(USAGE_LOG, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # 跨进程安全
        f.write(json.dumps(entry) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)

    # 日志轮转
    if os.path.getsize(USAGE_LOG) > MAX_LOG_SIZE:
        rotate_log(USAGE_LOG)

def detect_skill_from_path(tool_name, tool_input):
    """识别 agent 用 Read 工具读取 .skill.md 的行为，自动记录使用。
    （agent-ml-research 模式）"""
    if tool_name == "Read" and ".skill.md" in str(tool_input):
        skill_name = extract_skill_name_from_path(tool_input)
        if skill_name:
            log_skill_usage(skill_name, "read", ...)
```

### 6.5 记忆与 Skill 的隔离

**问题**（Hermes 发现）：当 skill 被调用时，展开的 prompt 包含完整 skill body。如果这被存入语义记忆，会污染未来的搜索结果。

**解决方案**：

```python
def extract_user_instruction_from_skill_message(message):
    """Strip skill scaffolding, keep only user's actual instruction.
    （Hermes scaffolding extraction 模式）
    
    存入记忆的是用户的原始意图，不是 skill 内容。
    """
    # 移除 skill body markers
    # 保留 user instruction 部分
    return cleaned_instruction
```

## 7. Skill 生命周期

```
创建                       Agent 创建 / Distill 生成 / 用户手写 / skill-creator
  ↓
草稿（pending/）            审批门控（可选，ccm config 控制）
  ↓
激活（active）              注册到 registry，可被发现和加载
  ↓
使用                       usage_tracker.jsonl 记录每次加载
  ↓
进化                       即时环追加经验 / 周期环整理
  ↓
30 天未使用 → stale         标记，但不影响功能
  ↓
使用 → 复活为 active        stale 状态被使用后自动恢复
  ↓
90 天未使用 → archived      移到 .archive/，从 registry 移除
  ↓
永不自动删除                人工确认后才删除

合并时：
  absorbed_into: umbrella-skill    # 记录被哪个 skill 吸收（Hermes）
  → 驱动自动引用迁移
```

**安全措施**：
- 原子写入（Hermes）：`tempfile + os.replace()`，失败不产生残缺文件
- 修改前备份（agent-ml-research）：`.bak` 文件
- Pinning（Hermes）：`pinned: true` 的 skill 不受 curator 影响，但可以被 patch
- Curator 备份（Hermes）：每次 curator 运行前完整备份到 `.curator_backups/`
- Dry-run 模式（Hermes）：`$curator --dry-run` 只报告不执行
- Plugin 安全（agent-ml-research）：AST 解析检查 blocklist 模式

## 8. 前端改造

### 8.1 Skill 列表 API

```
GET /api/skills
→ [{ name, description, scope, always, enabled, tags, commands, heavy, version }]

GET /api/skills/{name}
→ { name, description, body, ccm: {...}, ... }  # L2 完整加载

PUT /api/tasks/{id}/skills
→ { enabled_skills: { "code-review": true, "monitor": true } }

GET /api/skills/usage
→ { skills: [{ name, total_uses, last_used, trigger_breakdown }] }
```

前端从 API 动态获取 skill 列表，不再硬编码 `ALL_TOOLS`。

### 8.2 Task Skill 面板

```
┌─ Skills ──────────────────────────────────────┐
│ ✓ code-review   审查代码质量            v3    │
│ ✓ monitor       后台监控               v2     │
│ ○ deploy        部署流程               v1     │
│ ○ test-runner   自动化测试  ⚠️ 5.2KB   v1    │
│                                               │
│ Usage: code-review (45次) monitor (23次)      │
│                                               │
│ [+ Create Skill]  [⚙ Curator Status]         │
└───────────────────────────────────────────────┘
```

## 9. 现有系统改造路径

### Phase 1：格式统一（1-2 天）
- 将现有 3 个 skill（help、workflows、monitor）迁移为 `.skill.md` 文件
- 实现 `discover_skills()` 替代硬编码 `ALL_TOOLS` 和 `COMMAND_REGISTRY`
- 前端从 API 获取 skill 列表
- Worker skill 注入独立 budget

### Phase 2：自动命令注册 + 触发（2-3 天）
- Skill 的 `ccm.commands` 自动注册
- 实现 `ccm.triggers` 自然语言触发
- Progressive Disclosure 三层加载
- 记忆隔离（scaffolding extraction）

### Phase 3：即时进化（2-3 天）
- 实现 `evolution.py`（失败反思 + 经验追加）
- 使用追踪（`skill_usage.jsonl`）
- 工具流自动检测（detect_skill_from_path）
- 前端 skill 使用统计面板

### Phase 4：周期进化（3-5 天）
- Curator 确定性状态转换（active → stale → archived）
- Distill 6 阶段流程（发现 → 确认 → 创建）
- Consolidate LLM 驱动合并（前缀聚类 + umbrella skill）
- 审批门控 UI + dry-run 模式
- 调度系统（interval + idle + 项目年龄检查）

### Phase 5：优化环 + Skill Creator（可选，后续）
- 对比分析优化（SkillEvolver 模式）
- Skill Creator 交互式创建流程（带 eval + benchmark）
- 9 项审计检查
- Plugin 热加载系统

## 10. 关键设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| Skill 格式 | SKILL.md + YAML frontmatter | 兼容 Agent Skills 开放标准，Claude Code / Hermes / MiMo 通用 |
| 进化去重 | 词重叠 60%（无 LLM） | agent-ml-research 实测有效；LLM 去重被移除说明收益不大 |
| Cooldown 持久化 | JSON 文件 | 进程重启不丢失（agent-ml-research 设计） |
| Curator 首次运行 | 延迟一个周期 | 防止新安装激进整理（Hermes 设计） |
| Description 原则 | WHEN not WHAT | MiMo CSO 测试证实 agent 走描述捷径 |
| Budget 溢出 | 第一个 skill 不受限 | 防止高优先级大 skill 被静默丢弃（agent-ml-research） |
| Worker budget | 独立 6000 chars | Worker 需要更具体操作指南（agent-ml-research） |
| 合并追踪 | absorbed_into 字段 | 自动迁移引用，防止合并断链（Hermes） |
| 安全 | 多层：原子写入 + 备份 + pin + dry-run | 融合 Hermes（最严格）+ agent-ml-research（实用） |

## 11. 参考来源

| 项目 | 链接 | 主要借鉴 |
|------|------|---------|
| agent-ml-research | [GitHub](https://github.com/caoxiaoyuyuyuyuyu/agent-ml-research) | Skill 格式、失败反思进化（源码级）、使用追踪、Budget 控制、三层 CLAUDE.md、Worker budget |
| Hermes Agent | [GitHub](https://github.com/NousResearch/hermes-agent) | Progressive Disclosure、Curator（确定性+LLM）、原子写入、absorbed_into、scaffolding extraction、pinning、dry-run |
| Hermes Self-Evolution | [GitHub](https://github.com/NousResearch/hermes-agent-self-evolution) | GEPA 遗传优化、trace 分析 |
| MiMo Code | [GitHub](https://github.com/XiaomiMiMo/MiMo-Code) | Dream/Distill 双循环（6阶段）、调度系统（interval+idle+年龄）、CSO 描述优化、hook 系统 |
| Claude Code Skills | [Docs](https://code.claude.com/docs/en/skills) | SKILL.md 标准格式、context budget、scope hierarchy |
| Agent Skills 标准 | [agentskills.io](https://agentskills.io) | 跨平台兼容格式 |
| SkillEvolver | [arXiv:2605.10500](https://arxiv.org/abs/2605.10500) | 对比分析优化、9 项审计、策略多样化 |
| EvoSkills | [arXiv:2604.01687](https://arxiv.org/abs/2604.01687) | 协同进化验证、信息隔离 |
| ASG-SI | [arXiv:2512.23760](https://arxiv.org/abs/2512.23760) | Skill 依赖图、可验证奖励、预算分配 |

## 12. 设计验证：发现的问题与修正决策

### 🔴 关键问题

#### 12.1 文件命名矛盾：SKILL.md vs .skill.md

**问题**：设计中混用了两种格式——目录结构用 `code-review/SKILL.md`（Claude Code 标准），代码引用用 `code-review.skill.md`（agent-ml-research 格式）。`detect_skill_from_path` 检查 `.skill.md` 但 `discover_skills` 遍历子目录找 `SKILL.md`，互相不兼容。

**决策**：**统一用子目录 + `SKILL.md` 格式（Claude Code 标准）**。理由：
- 兼容 Claude Code 原生 skill 系统
- 子目录可放 `scripts/`、`references/` 等附属文件
- `detect_skill_from_path` 改为检查路径中是否包含 `/skills/` 且文件名为 `SKILL.md`

#### 12.2 Claude Code 原生 skill 冲突

**问题**：如果 CCM 的 skill 文件放在 `~/.claude/skills/`，Claude Code 会自动发现并加载它们到自己的 context。CCM 同时通过 L1 注入相同内容，导致 context 中出现双份，浪费 token。更严重的是，CC 的原生 `when_to_use` 匹配可能和 CCM 的 `triggers` 同时触发，产生冲突行为。

**决策**：**CCM skills 放在独立目录 `~/.ccm/skills/`，不放 `~/.claude/skills/`**。
- 避免和 CC 原生 skill 冲突
- CCM 通过自己的注入机制控制 skill 加载
- 如果用户同时安装了 CC 原生 skill 和 CCM skill，两者独立不干扰
- 项目级 skills 放在 `{project}/.ccm/skills/`（不是 `.claude/skills/`）

```
~/.ccm/skills/                    # CCM 全局 skills（不被 CC 扫描）
  code-review/
    SKILL.md
{project}/.ccm/skills/            # CCM 项目级 skills
  deploy/
    SKILL.md
```

#### 12.3 triggers 匹配机制

**问题**：设计声明了 `ccm.triggers` 功能但完全没定义实现——谁匹配、什么算法、多匹配怎么办。

**决策**：**Phase 1 不实现 triggers，Phase 2 用简单子串匹配 + 优先级**。

```python
# Phase 2 实现方案
def match_triggers(user_message: str, skills: dict) -> list[Skill]:
    """简单子串匹配，中英文均适用。"""
    matched = []
    msg_lower = user_message.lower()
    for skill in skills.values():
        for trigger in skill.ccm.get("triggers", []):
            if trigger.lower() in msg_lower:
                matched.append(skill)
                break  # 每个 skill 只匹配一次
    # 按 priority 降序，只取 top 3 避免过度加载
    return sorted(matched, key=lambda s: s.ccm.priority, reverse=True)[:3]
```

- 匹配在 CCM 后端 Python 层执行，不依赖 LLM
- 每条用户消息检查一次（在 `send_prompt` 之前）
- 多匹配取 top 3（按 priority）
- 中文子串匹配天然可用（不需要分词）
- 后续可升级为 embedding 相似度

#### 12.4 进化写入原子性

**问题**：设计声称用 `tempfile + os.replace()` 做原子写入，但伪代码是 `prepend_lesson()` 直接修改文件——进程崩溃会腐蚀文件。agent-ml-research 的源码也是非原子的。

**决策**：**实际实现时必须用 tempfile + os.replace()**。

```python
def prepend_lesson(skill_path: Path, lesson: str):
    """原子写入（真正的）。"""
    content = skill_path.read_text()
    # 在 "## 经验教训" 后插入新教训
    updated = insert_lesson(content, lesson)
    # 先写临时文件，再原子替换
    tmp = skill_path.with_suffix(".skill.md.tmp")
    tmp.write_text(updated)
    os.replace(tmp, skill_path)  # 原子操作
```

### 🟡 重要缺陷

#### 12.5 进化并发文件锁

**问题**：两个 session 同时对同一 skill 触发 evolution，后写的会覆盖先写的教训。cooldown 检查有 TOCTOU 竞态。

**决策**：**使用 `fcntl.flock` 对每个 skill 文件加写锁**。

```python
async def evolve_on_failure(tool_name, error, context, skill_registry):
    related_skill = find_related_skill(tool_name, skill_registry)
    if not related_skill:
        return

    lock_path = related_skill.path.with_suffix(".lock")
    with open(lock_path, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 非阻塞
        except BlockingIOError:
            return  # 另一个 session 正在进化，跳过

        # cooldown + reflect + dedup + write（在锁内完成）
        ...
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
```

#### 12.6 多版本备份

**问题**：单个 `.bak` 在连续腐蚀时会丢失最后一个好备份。

**决策**：**保留最近 3 个带时间戳的备份**。

```
code-review/
  SKILL.md
  .backups/
    SKILL.md.2026-06-17T10:30:00
    SKILL.md.2026-06-16T15:20:00
    SKILL.md.2026-06-15T08:45:00   # 最多保留 3 个
```

#### 12.7 Worker skill 同步

**问题**：Worker 和 Manager 的 skill 文件独立，进化后不同步。

**决策**：**Skills 跟随 CCM 代码部署，不独立同步。Worker 的进化教训上报 Manager。**

- skill 文件放在 CCM 仓库的 `skills/` 目录中（不是 `~/.ccm/skills/`）
- Worker bootstrap 时 rsync 自动包含
- Worker 上的进化教训通过 relay 事件上报 Manager
- Manager 收到后写入本地 skill 文件
- 下次 Worker 重启/重新部署时自动获取最新 skill

修正后的目录结构：
```
{ccm_repo}/skills/global/           # 仓库内全局 skills（rsync 部署）
  code-review/SKILL.md
  monitor/SKILL.md
{project}/.ccm/skills/              # 项目级 skills（rsync 项目目录时包含）
  deploy/SKILL.md
```

#### 12.8 Curator/Distill 归档引用断裂

**问题**：Curator 归档 Skill A → Distill 创建 Skill C 引用 Skill A → 引用断裂。

**决策**：
- Distill 第 5 步（筛选）增加检查：新 skill 引用的所有 skill 必须是 active 状态
- Curator 归档前检查：被其他 active skill 引用的 skill 不归档（加入 `referenced_by` 计数）
- Phase 4 实现时加入简单的引用检查（grep skill body 中是否提到其他 skill name）

#### 12.9 中文去重

**问题**：词重叠按空格分词，中文句子是一个"词"，两条语义相同但措辞不同的中文教训会被判为不重复。

**决策**：**使用字符级 n-gram 重叠代替词重叠**。

```python
def is_duplicate(new_lesson: str, existing: list[str], threshold=0.5) -> bool:
    """字符级 bigram 重叠，中英文通用。"""
    def bigrams(text):
        text = re.sub(r'\s+', '', text.lower())  # 去空白统一小写
        return set(text[i:i+2] for i in range(len(text)-1))

    new_bg = bigrams(new_lesson)
    for old in existing:
        old_bg = bigrams(old)
        if not new_bg or not old_bg:
            continue
        overlap = len(new_bg & old_bg) / min(len(new_bg), len(old_bg))
        if overlap > threshold:
            return True
    return False
```

- bigram 对中文天然有效（每两个字一组）
- 阈值从 0.6 调整为 0.5（bigram 比词重叠更精细）
- `min()` 分母避免短句被长句稀释

#### 12.10 MCP 工具动态注册

**问题**：FastMCP 的 MCP server 不支持运行时动态添加工具。Skill 的 `ccm.commands` 不能直接注册为 MCP tool。

**决策**：**Skill commands 走 CCM 命令系统（$command），不走 MCP 工具注册**。

- `ccm_skills_server` 保留核心工具（help、enable/disable、monitor）
- Skill 定义的 commands 由 CCM 的命令 dispatcher 处理
- Agent 通过 `$command` 前缀触发，或自然语言匹配 triggers 触发
- 不需要动态注册 MCP 工具

#### 12.11 DB 模型兼容

**问题**：`Task.enabled_skills` 是扁平 `{name: bool}` JSON，新系统需要更多元数据。

**决策**：**Phase 1 保持扁平 JSON 兼容**，只存启用/禁用状态。Skill 的完整元数据由 SKILL.md 文件提供，不存 DB。

```python
# 保持现有模型不变
enabled_skills: Mapped[dict | None]  # {"code-review": true, "monitor": true}

# Skill 元数据从文件系统读取，不从 DB 读
# 前端调 GET /api/skills 获取完整列表（从文件系统）
# 前端调 PUT /api/tasks/{id} 只传 enabled_skills: {name: bool}
```

### 🟢 改进点

#### 12.12 harness_exclude 动态禁用

**来自 agent-ml-research**：后端可临时禁用特定 skill（不修改 SKILL.md）。

**决策**：`discover_skills()` 增加 `exclude: set[str]` 参数。CCM 后端在特定条件下（如调试模式、Worker 限制）传入排除列表。

#### 12.13 经验教训区 section header

**问题**：agent-ml-research 用 `## 经验沉淀`，设计文档用 `## 经验教训`。

**决策**：统一用 `## Lessons Learned`（英文，避免中英混杂；且 agent-ml-research 的实际实现也支持英文 header）。

#### 12.14 detect_skill_from_path 在 PTY 架构中不可行

**问题**：CCM 通过 JSONL 外部观察 CC 的工具调用，JSONL 中的 `tool_use` block 可能不包含 Read 工具的完整文件路径。

**决策**：**不在 PTY 层实现 detect_skill_from_path**。改为在 CCM 的 `send_prompt` 触发点记录 skill 使用（如果 prompt 经过了 trigger 匹配或 command 解析，就记录对应 skill）。

#### 12.15 Skill 依赖声明

**来自 ASG-SI 论文**：显式声明 skill 间依赖关系。

**决策**：**Phase 4+ 考虑**。在 SKILL.md frontmatter 增加 `depends_on: [skill-name]`。Curator 归档时检查依赖链。Phase 1-3 用简单的 body grep 做弱引用检查。
