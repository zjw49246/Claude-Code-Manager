# CCM Skills 系统设计方案

> 基于 agent-ml-research、Hermes Agent、MiMo Code、Claude Code 原生 skills、SkillEvolver 等项目的深度调研，融合设计。

## 1. 设计目标

- Skills 可自动发现、按需加载、自主进化
- 兼容 [Agent Skills 开放标准](https://agentskills.io)（Claude Code / Hermes / MiMo / Codex CLI 共用的 SKILL.md 格式）
- Commands 和 Skills 互补但不混淆
- 前端从 API 动态获取，不再硬编码
- 三环自进化：即时反思 → 周期整理 → 按需优化

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

```
~/.claude/skills/              # 用户级 skills（Claude Code 标准路径）
  code-review/
    SKILL.md
    scripts/                   # 可选脚本
    references/                # 可选参考文档（按需加载）
  monitor/
    SKILL.md
    scripts/

{project}/.claude/skills/      # 项目级 skills（覆盖用户级同名 skill）
  deploy/
    SKILL.md
```

### 3.2 SKILL.md 格式

```yaml
---
# === Agent Skills 标准字段 ===
name: code-review
description: >
  审查代码质量：安全性（OWASP Top 10）、性能、可维护性。
  当用户提到"审查"、"review"、"检查代码"时自动激活。
when_to_use: >
  用户要求审查代码、提交 PR 前检查、代码质量评估时使用。
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
  tags: [quality, review]
  roles: []                  # 空=所有角色可用
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
<!-- 自进化系统自动追加 -->
```

### 3.3 字段说明

| 字段 | 来源 | 说明 |
|------|------|------|
| `name`, `description`, `when_to_use` | Claude Code 标准 | 驱动自动触发；description 限 1536 chars |
| `arguments` | Claude Code 标准 | 命名参数，支持 `$name` 替换 |
| `allowed-tools` / `disallowed-tools` | Claude Code 标准 | Skill 激活时的工具权限 |
| `ccm.always` | agent-ml-research | 是否自动注入 system prompt |
| `ccm.priority` | agent-ml-research | Budget 满时的取舍优先级 |
| `ccm.tools` | agent-ml-research | 关联工具，用于失败反思定位 |
| `ccm.commands` | Hermes | 自动注册为 CCM 命令 |
| `ccm.triggers` | 新增 | 自然语言触发词 |
| `ccm.tags` | 通用 | 分类标签 |

## 4. 发现与注入机制

### 4.1 三层渐进加载

借鉴 Hermes 的 Progressive Disclosure + agent-ml-research 的 Budget 控制 + Claude Code 的 context budget：

```
Session 启动
  │
  ├─ L0：元数据注入（始终在 system prompt 中）
  │     所有 skill 的 name + description（一行摘要）
  │     Budget：context window 的 1%（Claude Code 标准）
  │     溢出时按 priority 排序，低优先级的 description 降级为仅 name
  │
  ├─ L1：always 注入
  │     ccm.always: true 的 skill 全文注入 system prompt
  │     Budget：max_always_prompt_chars（默认 4000 chars）
  │     最多 max_always_in_prompt 个（默认 10）
  │
  └─ L2：按需加载
        触发方式：
        ├─ 用户 /command 或 $command → 加载关联 skill
        ├─ 自然语言匹配 ccm.triggers → 自动加载
        └─ Agent 主动 Read skill 文件
        加载后持久到 session 结束
        Context compaction 后重新附加前 5000 tokens/skill
```

### 4.2 发现流程

```python
def discover_skills(config_dir, project_dir, role=None, mode=None):
    """扫描 skill 目录，构建 registry。"""
    skills = {}

    # 用户级 skills
    user_dir = Path(config_dir) / "skills"
    for skill_dir in user_dir.iterdir():
        if (skill_dir / "SKILL.md").exists():
            skills[skill_dir.name] = parse_skill(skill_dir / "SKILL.md")

    # 项目级 skills（覆盖同名）
    project_dir = Path(project_dir) / ".claude" / "skills"
    for skill_dir in project_dir.iterdir():
        if (skill_dir / "SKILL.md").exists():
            skills[skill_dir.name] = parse_skill(skill_dir / "SKILL.md")

    # 过滤：role、mode
    if role:
        skills = {k: v for k, v in skills.items()
                  if not v.ccm.roles or role in v.ccm.roles}

    return skills
```

## 5. 命令系统

### 5.1 命令与 Skill 的关系

```
        ┌──────────────────────────────────────────┐
        │             命令来源                       │
        │                                           │
        │  1. 内置命令（Python 代码，管理系统状态）     │
        │     $help, $status, $stop, $config         │
        │     → 不关联 skill，直接执行 Python 逻辑     │
        │                                           │
        │  2. Skill 自动命令（SKILL.md ccm.commands） │
        │     $review, $monitor, $deploy             │
        │     → 加载 skill 内容作为上下文 + 执行逻辑   │
        │                                           │
        │  3. Agent 创建的命令（skill-creator 生成）    │
        │     $run-tests, $check-lint                │
        │     → 完全由 SKILL.md 定义                  │
        └──────────────────────────────────────────┘
```

### 5.2 内置命令注册

```python
# 保留现有的 decorator 模式（来自 agent-ml-research）
@command(
    name="help",
    patterns=(r"\$help\s*(.*)",),
    always_available=True,
)
def handle_help(ctx: CommandContext) -> str:
    """列出所有可用命令和 skills。"""
    ...
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
┌─────────────────────────────────────────────────────────┐
│                    三环自进化                              │
│                                                          │
│  即时环（每次失败）          ← agent-ml-research           │
│  ├─ 触发：工具执行失败 / 任务失败                           │
│  ├─ 动作：LLM 反思 → 追加经验教训到关联 skill               │
│  ├─ 节流：同一 skill 600s 内不重复                         │
│  ├─ 去重：新教训与已有教训词重叠 > 70% 则跳过               │
│  └─ 安全：写入前创建 .bak 备份                             │
│                                                          │
│  周期环（定期）              ← MiMo + Hermes Curator       │
│  ├─ 触发条件：                                            │
│  │   ├─ 距上次运行 >= interval_hours（默认 168h = 7 天）   │
│  │   └─ 当前空闲 >= min_idle_hours（默认 2h）              │
│  ├─ Phase 1 - 确定性整理（Hermes Curator）：               │
│  │   ├─ 30 天未使用 → stale                               │
│  │   ├─ 90 天未使用 → archived（移到 .archive/）           │
│  │   └─ 永不自动删除                                      │
│  ├─ Phase 2 - Distill（MiMo）：                           │
│  │   ├─ 分析近期对话历史                                   │
│  │   ├─ 识别重复手动操作模式                               │
│  │   └─ 生成新 skill 候选 → 审批门控                       │
│  └─ Phase 3 - Consolidate（Hermes Curator LLM 驱动）：     │
│      ├─ 审查 agent 创建的 skills                           │
│      ├─ 合并重叠的 skills                                  │
│      └─ max_iterations = 8（防止无限循环）                  │
│                                                          │
│  优化环（按需）              ← Hermes + SkillEvolver       │
│  ├─ 触发：人工执行 /optimize-skills                        │
│  ├─ 方法：对比分析（高效 trace vs 低效 trace）               │
│  │   delta = features(成功执行) - features(失败执行)        │
│  │   → 定向修改 skill（保留有效部分，补充缺失约束）          │
│  ├─ 验证：修改前后各 5 次独立测试对比                       │
│  └─ 审计：9 项检查（内容泄露 1-6、部署失败 7-9）            │
└─────────────────────────────────────────────────────────┘
```

### 6.2 即时环：失败反思

```python
# core/skills/evolution.py（参考 agent-ml-research）

async def evolve_on_failure(
    tool_name: str,
    error: str,
    context: str,
    skill_registry: dict,
):
    """工具执行失败时，反思并追加经验到关联 skill。"""

    # 1. 找关联 skill
    #    优先：skill.ccm.tools 包含该工具
    #    其次：skill.ccm.tags 匹配
    #    最后：skill.name 子串匹配
    related_skill = find_related_skill(tool_name, skill_registry)
    if not related_skill:
        return

    # 2. 节流检查
    if recently_evolved(related_skill, cooldown=600):
        return

    # 3. LLM 反思
    lesson = await reflect_on_failure(
        tool_name=tool_name,
        error=error,
        context=context,
        existing_lessons=related_skill.lessons,
        model="claude-haiku-4-5",  # 用轻量模型
    )

    # 4. 跳过瞬时错误
    if lesson == "SKIP":
        return

    # 5. 去重
    if is_duplicate(lesson, related_skill.lessons, threshold=0.7):
        return

    # 6. 追加（原子写入 + 备份）
    backup_skill(related_skill)
    append_lesson(related_skill, lesson, timestamp=now())
    log_evolution(related_skill, tool_name, lesson)
```

### 6.3 周期环：Curator + Distill

```python
# 触发条件（Hermes Curator 模式）
def should_run_curator(last_run: datetime, idle_since: datetime) -> bool:
    hours_since_run = (now() - last_run).total_hours
    hours_idle = (now() - idle_since).total_hours
    return hours_since_run >= 168 and hours_idle >= 2  # 7天 + 2小时空闲

# Phase 1: 确定性状态转换
def deterministic_transitions(skills: dict, usage_tracker):
    for skill in skills.values():
        if skill.created_by != "agent":
            continue  # 只管理 agent 创建的 skills
        days_unused = usage_tracker.days_since_last_use(skill.name)
        if days_unused >= 90:
            archive_skill(skill)  # 移到 .archive/，不删除
        elif days_unused >= 30:
            mark_stale(skill)

# Phase 2: Distill（分析对话历史，提炼新 skill）
async def distill(conversation_history, existing_skills):
    """识别重复模式，生成新 skill 候选。"""
    patterns = analyze_patterns(conversation_history)
    candidates = []
    for pattern in patterns:
        if pattern.frequency >= 3 and pattern.confidence >= 0.8:
            skill_md = generate_skill_md(pattern)
            candidates.append(skill_md)
    return candidates  # 进入审批门控
```

### 6.4 使用追踪

```python
# core/skills/tracker.py（参考 agent-ml-research）

def log_skill_usage(skill_name, trigger_type, project, session_id):
    """每次 skill 加载时记录。"""
    entry = {
        "timestamp": now().isoformat(),
        "skill": skill_name,
        "trigger": trigger_type,  # "inject" | "read" | "command" | "trigger"
        "project": project,
        "session": session_id,
    }
    append_jsonl("skill_usage.jsonl", entry)

# 日志轮转：5MB 上限
# 聚合查询：summarize() 返回使用频率、趋势
```

## 7. Skill 生命周期

```
创建                    Agent 创建 / Distill 生成 / 用户手写
  ↓
草稿（pending/）         审批门控（可选，ccm config 控制）
  ↓
激活                    注册到 registry，可被发现和加载
  ↓
使用                    usage_tracker.jsonl 记录每次加载
  ↓
进化                    即时环追加经验 / 周期环整理
  ↓
30 天未使用 → stale      标记，但不影响功能
  ↓
90 天未使用 → archived   移到 .archive/，从 registry 移除
  ↓
永不自动删除             人工确认后才删除
```

**安全措施**：
- 原子写入（Hermes）：`tempfile + os.replace()`
- 修改前备份（agent-ml-research）：`.bak` 文件
- Pinning（Hermes）：`pinned: true` 的 skill 不受 curator 影响
- Curator 备份（Hermes）：每次 curator 运行前完整备份

## 8. 前端改造

### 8.1 Skill 列表 API

```
GET /api/skills
→ [{ name, description, scope, always, enabled, tags, commands }]

PUT /api/tasks/{id}/skills
→ { enabled_skills: { "code-review": true, "monitor": true } }
```

前端从 API 动态获取 skill 列表，不再硬编码 `ALL_TOOLS`。

### 8.2 Task Skill 面板

```
┌─ Skills ──────────────────────────┐
│ ✓ code-review  审查代码质量       │
│ ✓ monitor      后台监控          │
│ ○ deploy       部署流程          │
│ ○ test-runner  自动化测试        │
│                                  │
│ [+ Create Skill]                 │
└──────────────────────────────────┘
```

## 9. 现有系统改造路径

### Phase 1：格式统一（1-2 天）
- 将现有 3 个 skill（help、workflows、monitor）迁移为 `.skill.md` 文件
- 实现 `discover_skills()` 替代硬编码 `ALL_TOOLS` 和 `COMMAND_REGISTRY`
- 前端从 API 获取 skill 列表

### Phase 2：自动命令注册 + 触发（2-3 天）
- Skill 的 `ccm.commands` 自动注册
- 实现 `ccm.triggers` 自然语言触发
- Progressive Disclosure 三层加载

### Phase 3：即时进化（2-3 天）
- 实现 `evolution.py`（失败反思 + 经验追加）
- 使用追踪（`skill_usage.jsonl`）
- 前端 skill 使用统计面板

### Phase 4：周期进化（3-5 天）
- Curator（确定性状态转换 + LLM 整理）
- Distill（对话历史分析 → 生成新 skill）
- 审批门控 UI

### Phase 5：优化环 + Skill Creator（可选，后续）
- 对比分析优化
- Skill Creator 交互式创建流程
- Eval 系统

## 10. 参考来源

| 项目 | 链接 | 主要借鉴 |
|------|------|---------|
| agent-ml-research | [GitHub](https://github.com/caoxiaoyuyuyuyuyu/agent-ml-research) | Skill 格式、失败反思进化、使用追踪、Budget 控制 |
| Hermes Agent | [GitHub](https://github.com/NousResearch/hermes-agent) | Progressive Disclosure、Curator、原子写入、安全扫描 |
| Hermes Self-Evolution | [GitHub](https://github.com/NousResearch/hermes-agent-self-evolution) | GEPA 遗传优化、trace 分析 |
| MiMo Code | [GitHub](https://github.com/XiaomiMiMo/MiMo-Code) | Dream/Distill 双循环、四层记忆 |
| Claude Code Skills | [Docs](https://code.claude.com/docs/en/skills) | SKILL.md 标准格式、skill-creator |
| Agent Skills 标准 | [agentskills.io](https://agentskills.io) | 跨平台兼容格式 |
| SkillEvolver | [arXiv:2605.10500](https://arxiv.org/abs/2605.10500) | 对比分析优化、9 项审计 |
| EvoSkills | [arXiv:2604.01687](https://arxiv.org/abs/2604.01687) | 协同进化验证 |
| ASG-SI | [arXiv:2512.23760](https://arxiv.org/abs/2512.23760) | Skill 依赖图、可验证奖励 |
