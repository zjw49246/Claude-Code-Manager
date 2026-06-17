# CCM Skills 系统升级概述

> 面向需求层面的改动说明。技术实现细节见 `SKILLS-SYSTEM-DESIGN.md`。

## 为什么要改

当前 CCM 的 skill 系统有三个硬编码的"技能"（help、workflows、monitor），新增一个技能需要改 3 个文件（后端注册、前端列表、测试）。Agent 无法从使用经验中学习，也无法自主创建新技能。

改造后，skills 变成可插拔的知识文档（标准 SKILL.md 格式），支持自动发现、动态加载、自主进化。

## 改什么

### 1. Skill 从硬编码变成文件

**现在**：技能定义写死在 Python 代码里（`command_registry.py`），新增技能要改代码、重新部署。

**改后**：每个技能是一个 `SKILL.md` 文件，放在 `skills/` 目录下。新增技能只需创建一个文件，零代码改动。

```
skills/
  monitor/SKILL.md      ← 监控技能（已迁移）
  code-review/SKILL.md  ← 代码审查（新增示例）
  deploy/SKILL.md       ← 部署流程（新增示例）
```

> **参考**：agent-ml-research 的 `skills/global/*.skill.md` 架构 + Claude Code 原生的 SKILL.md 标准格式（[Agent Skills 开放标准](https://agentskills.io)）。

### 2. Agent 可以按需读取技能

**现在**：技能要么全量注入（占用 token），要么完全不可见。

**改后**：分三层加载——
- **L0**：所有技能的名字和简介始终在 system prompt 里（agent 知道有哪些技能可用）
- **L1**：标记为"常驻"的技能自动注入全文（有 token 预算控制，防止过度占用）
- **L2**：其他技能按需加载——agent 通过 MCP 工具 `ccm_read_skill(name)` 在需要时读取

> **参考**：agent-ml-research 的 budget 控制（4000 字符上限、最多 10 个常驻技能）+ Hermes Agent 的三层渐进加载（Progressive Disclosure）。

### 3. 技能和命令分离

**现在**：技能和命令混在一起。`$help` 被当作技能但其实是系统命令；`workflows` 被当作技能但其实是配置开关。

**改后**：
- **技能** = 知识文档（教 agent "什么时候做"和"怎么做"）
- **命令** = 用户触发的动作（`$help`、`$monitor`）
- **设置** = 配置开关（`enable_workflows`）

三者独立管理，不再混淆。技能可以声明关联命令（在 SKILL.md 里注册），命令可以加载技能作为上下文，但它们不是同一个东西。

> **参考**：agent-ml-research 的核心设计理念——"命令是用户触发的 Python 函数，技能是指导 agent 行为的知识文档，两者独立但互补"。

### 4. Agent 从失败中学习（自进化）

**现在**：agent 犯了错不会记住，下次可能重蹈覆辙。

**改后**：
- **即时反思**：工具执行失败时，系统自动用轻量 LLM 分析失败原因，生成简短教训存入数据库
- **定期整理**：每 7 天自动整理——30 天未使用的技能标记为过时，90 天的归档；同时分析近期使用模式，自动提炼新技能
- **按需优化**：管理员手动触发，对比成功/失败的执行 trace 优化技能内容

> **参考**：agent-ml-research 的工具失败自动反思（`evolution.py`，10 分钟冷却防刷，60% 字符重叠去重）+ MiMo Code 的 Dream/Distill 双循环（7 天整理 + 30 天提炼）+ Hermes Agent 的 Curator 生命周期管理（active → stale → archived，永不自动删除）。

### 5. Worker 自动获取技能

**现在**：Worker 上的技能配置和 Manager 可能不一致。

**改后**：技能文件在代码仓库里，Worker 部署时自动获取（rsync 自带）。Worker 上的学习经验通过 relay 事件回传 Manager，存入数据库。

> **参考**：agent-ml-research 的 Worker 独立 budget（6000 字符 vs 主 agent 4000 字符）。

### 6. 前端动态加载

**现在**：前端硬编码了 Help / Workflows / Monitor 三个选项。

**改后**：前端从 API 动态获取技能列表。新增技能后刷新页面即可看到，无需重新构建前端。

## 实施计划

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 技能文件化 + 发现机制 + API + 前端动态加载 | ✅ 已完成 |
| Phase 2 | MCP 工具扩展（ccm_read_skill）+ 命令自动注册 | 进行中 |
| Phase 3 | 即时进化（失败反思 → 教训入库） | 待开始 |
| Phase 4 | 周期进化（Curator 整理 + Distill 提炼） | 待开始 |
| Phase 5 | 高级功能（优化环、Skill Creator、Plugin 系统） | 后续 |

## 用户体验变化

| 操作 | 现在 | 改后 |
|------|------|------|
| 查看可用技能 | 固定 3 个选项 | 从 API 动态获取，显示所有 SKILL.md 定义的技能 |
| 启用/禁用技能 | ToolsBadge 勾选 | 不变（同样的 UI，同样的操作） |
| 使用 $command | `$monitor` 触发监控 | 不变（同样的命令语法） |
| Agent 自发现 | ccm_command_help 列出命令 | 增强：system prompt 里直接看到技能目录 |
| 新增技能 | 改 3 个代码文件 + 部署 | 创建 SKILL.md 文件即可 |

## 参考系统总结

| 参考项目 | 借鉴了什么 |
|---------|-----------|
| [agent-ml-research](https://github.com/caoxiaoyuyuyuyuyu/agent-ml-research) | SKILL.md 格式、budget 控制、失败反思进化、使用追踪、Worker budget 分离、命令与技能分离 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 渐进加载、Curator 生命周期管理、原子写入、合并追踪、审批门控 |
| [MiMo Code](https://github.com/XiaomiMiMo/MiMo-Code) | Dream/Distill 双循环、项目年龄检查、描述优化（WHEN not WHAT） |
| Claude Code 原生 | SKILL.md 标准格式、--append-system-prompt-file 注入 |
